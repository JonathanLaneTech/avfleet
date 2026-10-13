import json
import plistlib

from conftest import device

from avfleet.checks import build_check, check_types
from avfleet.models import Status

JSON = {"Content-Type": "application/json"}


def test_all_device_types_registered():
    for t in ("roon", "yamaha_yxc", "kaleidescape", "apple_tv", "homepod", "tv", "lutron_leap",
              "unifi_device", "tcp", "http"):
        assert t in check_types()


# --- Yamaha YXC ----------------------------------------------------------------------

def yamaha(fake_http, payload, status=200):
    fake_http.routes[("GET", "/YamahaExtendedControl/v1/main/getStatus")] = (status, json.dumps(payload), JSON)
    return build_check(device("yamaha_yxc", host=f"127.0.0.1:{fake_http.port}")).run()


def test_yamaha_on(fake_http):
    r = yamaha(fake_http, {"response_code": 0, "power": "on", "input": "net_radio", "volume": 40})
    assert r.status is Status.UP
    assert "net_radio" in r.detail
    assert r.metrics["volume"] == 40
    assert r.latency_ms is not None


def test_yamaha_standby_is_healthy(fake_http):
    r = yamaha(fake_http, {"response_code": 0, "power": "standby"})
    assert r.status is Status.UP and r.detail == "standby"


def test_yamaha_error_code_is_degraded(fake_http):
    assert yamaha(fake_http, {"response_code": 5}).status is Status.DEGRADED


def test_yamaha_http_error_is_down(fake_http):
    assert yamaha(fake_http, {}, status=500).status is Status.DOWN


def test_unreachable_device_is_down_not_exception(closed_port):
    r = build_check(device("yamaha_yxc", host=f"127.0.0.1:{closed_port}")).run()
    assert r.status is Status.DOWN
    assert "URLError" in r.detail or "Connection" in r.detail


# --- AirPlay (Apple TV / HomePod) ---------------------------------------------------

def test_apple_tv_info_plist(fake_http):
    body = plistlib.dumps({"model": "AppleTV14,1", "name": "Den", "osBuildVersion": "22K557"}, fmt=plistlib.FMT_BINARY)
    fake_http.routes[("GET", "/info")] = (200, body, {"Content-Type": "application/x-apple-binary-plist"})
    r = build_check(device("apple_tv", port=fake_http.port)).run()
    assert r.status is Status.UP
    assert r.metrics["model"] == "AppleTV14,1"
    assert "AppleTV14,1" in r.detail


def test_homepod_403_still_up(fake_http):
    fake_http.routes[("GET", "/info")] = (403, b"", {})
    r = build_check(device("homepod", port=fake_http.port)).run()
    assert r.status is Status.UP and "403" in r.detail


def test_airplay_500_down(fake_http):
    fake_http.routes[("GET", "/info")] = (500, b"", {})
    assert build_check(device("apple_tv", port=fake_http.port)).run().status is Status.DOWN


# --- TVs ----------------------------------------------------------------------------

def test_sony_bravia_power_and_psk(fake_http):
    fake_http.routes[("POST", "/sony/system")] = (200, json.dumps({"result": [{"status": "standby"}], "id": 50}), JSON)
    r = build_check(device("tv", host=f"127.0.0.1:{fake_http.port}", protocol="sony_bravia", psk="1234")).run()
    assert r.status is Status.UP and r.metrics["power"] == "standby"
    req = fake_http.requests[-1]
    assert req["headers"].get("x-auth-psk") == "1234"
    assert json.loads(req["body"])["method"] == "getPowerStatus"


def test_sony_bravia_auth_error_degraded(fake_http):
    fake_http.routes[("POST", "/sony/system")] = (200, json.dumps({"error": [403, "Forbidden"], "id": 50}), JSON)
    r = build_check(device("tv", host=f"127.0.0.1:{fake_http.port}", protocol="sony_bravia")).run()
    assert r.status is Status.DEGRADED


def test_samsung(fake_http):
    payload = {"device": {"PowerState": "on", "modelName": "QN65"}}
    fake_http.routes[("GET", "/api/v2/")] = (200, json.dumps(payload), JSON)
    r = build_check(device("tv", protocol="samsung", port=fake_http.port)).run()
    assert r.status is Status.UP and r.metrics["model"] == "QN65"


def test_tv_unknown_protocol():
    assert build_check(device("tv", protocol="betamax")).run().status is Status.UNKNOWN


# --- Generic HTTP -------------------------------------------------------------------

def test_http_check(fake_http):
    fake_http.routes[("GET", "/health")] = (200, b"ok", {})
    assert build_check(device("http", port=fake_http.port, path="/health")).run().status is Status.UP
    assert build_check(device("http", port=fake_http.port, path="/missing")).run().status is Status.DOWN
