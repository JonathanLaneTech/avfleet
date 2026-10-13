import json

from conftest import device

from avfleet.checks import build_check
from avfleet.checks.unifi import summarize_device
from avfleet.models import Status

SWITCH = {
    "mac": "02:00:00:00:00:02", "model": "USPM16P", "version": "7.1.26", "state": 1, "uptime": 86400,
    "num_sta": 9, "total_max_power": 180, "system-stats": {"cpu": "12.5", "mem": "40.1"},
    "port_table": [
        {"port_idx": 1, "up": True, "poe_power": "6.20"},
        {"port_idx": 2, "up": True, "poe_power": "15.75"},
        {"port_idx": 3, "up": False, "poe_power": "0"},
    ],
}


def test_summarize_switch_ports_and_poe():
    status, detail, m = summarize_device(SWITCH)
    assert status is Status.UP
    assert m["ports_up"] == 2 and m["ports_total"] == 3
    assert m["poe_watts"] == 21.9
    assert m["poe_budget_watts"] == 180
    assert m["cpu_pct"] == 12.5
    assert "9 clients" in detail and "2/3 ports up" in detail


def test_expected_port_down_is_degraded():
    status, detail, m = summarize_device(SWITCH, expected_up_ports=[1, 3])
    assert status is Status.DEGRADED
    assert m["expected_ports_down"] == [3]
    assert "3" in detail


def test_disconnected_is_down_upgrading_is_degraded():
    assert summarize_device({**SWITCH, "state": 0})[0] is Status.DOWN
    status, detail, _ = summarize_device({**SWITCH, "state": 4})
    assert status is Status.DEGRADED and detail == "upgrading"


def test_access_point_clients():
    status, detail, m = summarize_device({"mac": "x", "state": 1, "num_sta": 14, "model": "U7PROMAX"})
    assert status is Status.UP and detail == "14 clients" and m["clients"] == 14


def _controller(fake_http, mac, expire_first=False):
    state = {"logins": 0, "served": 0}

    def login(req):
        state["logins"] += 1
        creds = json.loads(req["body"])
        if creds != {"username": "monitor", "password": "s3cret"}:
            return 401, "{}", {}
        return 200, "{}", {"Set-Cookie": "TOKEN=abc; Path=/", "X-CSRF-Token": "csrf-1"}

    def stat(req):
        state["served"] += 1
        if expire_first and state["served"] == 1:
            return 401, "{}", {}
        if "TOKEN=abc" not in req["headers"].get("cookie", ""):
            return 401, "{}", {}
        return 200, json.dumps({"meta": {"rc": "ok"}, "data": [SWITCH]}), {"Content-Type": "application/json"}

    fake_http.routes[("POST", "/api/auth/login")] = login
    fake_http.routes[("GET", f"/proxy/network/api/s/default/stat/device/{mac}")] = stat
    return state


def _check(fake_http, **opts):
    base = dict(mac="02:00:00:00:00:02", username="monitor", password="s3cret",
                controller=f"http://127.0.0.1:{fake_http.port}")
    base.update(opts)
    return build_check(device("unifi_device", **base))


def test_unifi_end_to_end_with_session_reuse(fake_http):
    state = _controller(fake_http, "02:00:00:00:00:02")
    chk = _check(fake_http)
    assert chk.run().status is Status.UP
    assert chk.run().status is Status.UP
    assert state["logins"] == 1  # second check reused the session
    assert fake_http.requests[-1]["headers"].get("x-csrf-token") == "csrf-1"


def test_unifi_relogs_in_after_401(fake_http):
    state = _controller(fake_http, "02:00:00:00:00:02", expire_first=True)
    r = _check(fake_http).run()
    assert r.status is Status.UP
    assert state["logins"] == 2


def test_unifi_bad_password_is_down(fake_http):
    _controller(fake_http, "02:00:00:00:00:02")
    r = _check(fake_http, password="wrong").run()
    assert r.status is Status.DOWN and "login failed" in r.detail


def test_unifi_missing_credentials_unknown():
    r = build_check(device("unifi_device", mac="02:00:00:00:00:02")).run()
    assert r.status is Status.UNKNOWN
