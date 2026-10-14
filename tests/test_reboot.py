"""Manual reboot: Bravia action, shared guard (config, cooldown), dashboard endpoint, MCP tool."""

import json
import urllib.error
import urllib.request

import pytest
from conftest import device

from avfleet import reboot
from avfleet.actions import build_action
from avfleet.actions.base import Action, register_action
from avfleet.config import Config, DeviceConfig, RebootConfig, parse_config
from avfleet.mcp_server import FleetMCP
from avfleet.models import ActionResult
from avfleet.store import Store
from avfleet.web import build_status, start_server

T0 = 3_000_000.0
CALLS: list[str] = []


@register_action("reboot_test")
class FakeReboot(Action):
    def execute(self):
        CALLS.append(self.device.id)
        return ActionResult(self.name, True, "rebooting (test)")


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()


def cfg(cooldown=300, actions=("reboot",)):
    return Config(devices=[
        DeviceConfig(id="tv", name="Sony Bravia", type="tcp", host="h",
                     reboot=RebootConfig(action="reboot_test", cooldown_s=cooldown)),
        DeviceConfig(id="apple-tv", name="Apple TV", type="tcp", host="h"),
    ], mcp_actions=list(actions))


def test_bravia_reboot_sends_request_with_psk(fake_http):
    fake_http.routes[("POST", "/sony/system")] = (200, json.dumps({"result": [], "id": 10}), {})
    dev = device("tv", host=f"127.0.0.1:{fake_http.port}", protocol="sony_bravia", psk="1234")
    out = build_action("bravia_reboot", dev, {}).run()
    assert out.ok and "back in about a minute" in out.message
    req = fake_http.requests[-1]
    assert json.loads(req["body"])["method"] == "requestReboot" and req["headers"]["x-auth-psk"] == "1234"


def test_bravia_reboot_reports_auth_error(fake_http):
    fake_http.routes[("POST", "/sony/system")] = (200, json.dumps({"error": [403, "Forbidden"], "id": 10}), {})
    out = build_action("bravia_reboot", device("tv", host=f"127.0.0.1:{fake_http.port}"), {}).run()
    assert not out.ok and "pre-shared key" in out.message


def test_guard_cooldown_and_unconfigured():
    store, c = Store(":memory:"), cfg()
    assert reboot.run(store, c, "tv", via="dashboard", now=T0).ok
    with pytest.raises(reboot.RebootError, match="moments ago"):
        reboot.run(store, c, "tv", via="dashboard", now=T0 + 60)
    assert reboot.run(store, c, "tv", via="dashboard", now=T0 + 400).ok
    assert CALLS == ["tv", "tv"]
    with pytest.raises(reboot.RebootError, match="no reboot action"):
        reboot.run(store, c, "apple-tv", via="dashboard", now=T0)
    msgs = [e["message"] for e in store.events() if e["kind"] == "reboot"]
    assert msgs[0].startswith("Sony Bravia: reboot requested via dashboard")


def test_config_parses_reboot_table():
    c = parse_config({"devices": [{"id": "roon", "type": "roon", "host": "h",
                                   "reboot": {"action": "roon_restart", "label": "Restart Roon"}}]})
    assert c.devices[0].reboot.label == "Restart Roon" and c.devices[0].reboot.cooldown_s == 300


def test_dashboard_reboot_endpoint_and_status():
    c, store = cfg(), Store(":memory:")
    c.web_host, c.web_port = "127.0.0.1", 0
    httpd = start_server(c, store)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(dev):
        req = urllib.request.Request(base + "/api/reboot", data=json.dumps({"device": dev}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        status = build_status(c, store)
        assert status["devices"][0]["reboot"] == {"label": "Reboot"} and status["devices"][1]["reboot"] is None
        assert post("tv") == (200, {"ok": True, "message": "rebooting (test)"})
        assert post("tv")[0] == 409
        assert post("apple-tv")[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_reboot_tool_opt_in():
    store = Store(":memory:")
    mcp = FleetMCP(cfg(actions=("reboot",)), store, clock=lambda: T0)
    reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "reboot_device", "arguments": {"device": "tv"}}})
    assert reply["result"]["isError"] is False and CALLS == ["tv"]
    assert any("via Claude" in e["message"] for e in store.events())
    locked = FleetMCP(cfg(actions=("maintenance",)), store, clock=lambda: T0)
    names = {t["name"] for t in locked.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert "reboot_device" not in names
