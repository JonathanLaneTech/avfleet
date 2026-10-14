"""MCP server: protocol handshake, read tools, opt-in guarded actions, keypad press against a fake QSX."""

import io
import json

import pytest
from test_lutron import FakeLeap, qsx_routes

from avfleet.actions.base import Action, register_action
from avfleet.config import Config, DeviceConfig, RemediationConfig
from avfleet.mcp_server import FleetMCP, outages
from avfleet.models import ActionResult, CheckResult, Event, Status
from avfleet.store import Store

T0 = 2_000_000.0
RAN: list[str] = []


@register_action("mcp_test_restart")
class FakeRestart(Action):
    def execute(self):
        RAN.append(self.device.id)
        return ActionResult(self.name, True, "restarted (test)")


def keypad_routes():
    routes = qsx_routes()
    routes.update({
        "/area": {"Areas": [{"href": "/area/3", "Name": "Example Home"}, {"href": "/area/32", "Name": "Living Room"}]},
        "/area/32/associatedcontrolstation": {"ControlStations": [{
            "href": "/controlstation/630", "Name": "Virtual Keypad",
            "AssociatedGangedDevices": [{"Device": {"href": "/device/632", "DeviceType": "PhantomKeypad"}}]}]},
        "/device/632/buttongroup/expanded": {"ButtonGroupsExpanded": [{"Buttons": [
            {"href": "/button/640", "ButtonNumber": 1, "Name": "Button 1", "Engraving": {"Text": "Astro Natural Show"}},
            {"href": "/button/663", "ButtonNumber": 4, "Name": "Button 4", "Engraving": {"Text": "Room Off"}}]}]},
        "/button/640/commandprocessor": {},
    })
    return routes


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def fleet():
    RAN.clear()
    servers = []

    def make(actions=("maintenance",)):
        devices = [
            DeviceConfig(id="roon", name="Roon Core", type="tcp", host="127.0.0.1", group="Audio",
                         options={"port": 1},
                         remediation=RemediationConfig(action="mcp_test_restart", cooldown_s=900)),
            DeviceConfig(id="lutron", name="Lutron QSX", type="lutron_leap", host="192.0.2.40", group="Lighting"),
        ]
        cfg = Config(interval_s=60, devices=devices, mcp_actions=list(actions))
        store, clock = Store(":memory:"), Clock()

        def connect(host):
            srv = FakeLeap(keypad_routes())
            servers.append(srv)
            return srv.client()

        return FleetMCP(cfg, store, clock=clock, lutron_connect=connect), store, clock

    yield make
    for s in servers:
        s.close()


def call(mcp, name, args=None, mid=1):
    reply = mcp.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                        "params": {"name": name, "arguments": args or {}}})
    result = reply["result"]
    text = result["content"][0]["text"]
    return result["isError"], (text if result["isError"] else json.loads(text))


def test_initialize_echoes_protocol_and_lists_read_tools_only_by_default(fleet):
    mcp, _, _ = fleet(actions=())
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["serverInfo"]["name"] == "avfleet"
    assert "tools" in init["result"]["capabilities"]
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = {t["name"]: t for t in mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert set(tools) == {"fleet_status", "device_detail", "recent_events", "list_keypad_buttons"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())


def test_actions_appear_only_when_allowed(fleet):
    mcp, _, _ = fleet(actions=("maintenance", "self_healing", "lutron_buttons"))
    names = {t["name"] for t in mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert {"start_maintenance", "end_maintenance", "run_self_healing", "press_keypad_button"} <= names
    mcp2, _, _ = fleet(actions=("maintenance",))
    reply = mcp2.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "run_self_healing", "arguments": {"device": "roon"}}})
    assert reply["error"]["code"] == -32602 and RAN == []  # not allowed = doesn't exist


def test_unknown_tool_and_method(fleet):
    mcp, _, _ = fleet()
    r = mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope"}})
    assert r["error"]["code"] == -32602
    r = mcp.handle({"jsonrpc": "2.0", "id": 10, "method": "resources/list"})
    assert r["error"]["code"] == -32601
    assert mcp.handle({"jsonrpc": "2.0", "id": 11, "method": "ping"})["result"] == {}


def test_fleet_status_and_device_detail_with_outage(fleet):
    mcp, store, clock = fleet()
    for i, st in enumerate(["up", "down", "down", "down", "up", "up"]):
        store.add_result(CheckResult("roon", Status(st), latency_ms=5, detail=f"check {i}",
                                     timestamp=T0 - 600 + i * 60))
    store.add_event(Event("roon", "alert", "Roon Core is DOWN: refused", timestamp=T0 - 400))
    err, data = call(mcp, "fleet_status")
    assert not err and data["summary"]["up"] == 1
    roon = next(d for d in data["devices"] if d["id"] == "roon")
    assert roon["status"] == "up" and roon["uptime_24h_pct"] == 50.0
    err, detail = call(mcp, "device_detail", {"device": "roon"})
    assert not err
    assert len(detail["outages"]) == 1 and detail["outages"][0]["checks"] == 3
    assert detail["outages"][0]["minutes"] == 3.0
    assert detail["recent_events"][0]["message"].startswith("Roon Core is DOWN")


def test_device_errors_are_tool_errors_not_crashes(fleet):
    mcp, _, _ = fleet()
    err, text = call(mcp, "device_detail", {"device": "projector"})
    assert err and "unknown device 'projector'" in text and "roon" in text
    err, text = call(mcp, "device_detail", {})
    assert err and "missing argument" in text


def test_maintenance_via_mcp_is_logged(fleet):
    mcp, store, clock = fleet()
    err, data = call(mcp, "start_maintenance", {"device": "lutron", "minutes": 45, "reason": "transfer"})
    assert not err and data["started"] == ["lutron"]
    assert store.active_maintenance(T0 + 1)["lutron"]["reason"] == "transfer"
    err, data = call(mcp, "end_maintenance", {"device": "lutron"})
    assert data == {"ended": ["lutron"]}
    err, text = call(mcp, "start_maintenance", {"device": "lutron", "minutes": 0})
    assert err and "between" in text


def test_self_healing_respects_cooldown(fleet):
    mcp, store, clock = fleet(actions=("self_healing",))
    err, data = call(mcp, "run_self_healing", {"device": "roon"})
    assert not err and RAN == ["roon"]
    assert any("via Claude" in e["message"] for e in store.events())
    clock.t += 120
    err, text = call(mcp, "run_self_healing", {"device": "roon"})
    assert err and "cooldown" in text and RAN == ["roon"]
    err, text = call(mcp, "run_self_healing", {"device": "lutron"})
    assert err and "no self-healing" in text


def test_list_and_press_keypad_button(fleet, monkeypatch):
    monkeypatch.setattr("avfleet.checks.lutron.find_pairing", lambda host, *a: ("c", "k", None))
    mcp, store, _ = fleet(actions=("lutron_buttons",))
    err, data = call(mcp, "list_keypad_buttons")
    assert not err
    assert data["buttons"][0] == {"button": "640", "label": "Astro Natural Show", "number": 1,
                                  "keypad": "Virtual Keypad", "keypad_type": "PhantomKeypad", "area": "Living Room"}
    err, data = call(mcp, "press_keypad_button", {"button": "640"})
    assert not err and data["pressed"]["label"] == "Astro Natural Show"
    assert any("pressed 'Astro Natural Show'" in e["message"] for e in store.events())
    err, text = call(mcp, "press_keypad_button", {"button": "999"})
    assert err and "isn't a keypad button" in text


def test_outage_windows_ignore_maintenance_and_mark_ongoing():
    rows = [{"ts": 0, "status": "down", "detail": "x", "maintenance": 1},
            {"ts": 60, "status": "up", "detail": "", "maintenance": 0},
            {"ts": 120, "status": "down", "detail": "refused", "maintenance": 0},
            {"ts": 180, "status": "down", "detail": "refused", "maintenance": 0}]
    w = outages(rows, 60)
    assert len(w) == 1 and w[0]["ongoing"] and w[0]["checks"] == 2 and w[0]["first_detail"] == "refused"


def test_stdio_round_trip(fleet):
    mcp, _, _ = fleet()
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\nnot json\n")
    stdout = io.StringIO()
    mcp.serve(stdin, stdout)
    replies = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700
