import json
import urllib.error
import urllib.request

import pytest

from avfleet.config import Config, DeviceConfig
from avfleet.models import CheckResult, Event, Status
from avfleet.store import Store
from avfleet.web import build_status, start_server

NOW = 2_000_000.0


def add(store, status, ts, latency=10.0):
    store.add_result(CheckResult("dev", Status(status), latency_ms=latency, timestamp=ts))


def test_uptime_counts_degraded_as_up_and_ignores_unknown():
    s = Store(":memory:")
    for i, st in enumerate(["up", "up", "degraded", "down", "unknown"]):
        add(s, st, NOW - 100 + i)
    assert s.uptime("dev", now=NOW) == 75.0


def test_uptime_window_excludes_old_results():
    s = Store(":memory:")
    add(s, "down", NOW - 90000)
    add(s, "up", NOW - 10)
    assert s.uptime("dev", now=NOW) == 100.0
    assert s.uptime("other", now=NOW) is None


def test_latency_series_buckets():
    s = Store(":memory:")
    add(s, "up", NOW - 86000, latency=10)
    add(s, "up", NOW - 85999, latency=20)
    add(s, "up", NOW - 10, latency=40)
    series = s.latency_series("dev", now=NOW, points=24)
    assert len(series) == 24
    assert series[0] == 15.0 and series[-1] == 40.0
    assert series[10] is None


def test_last_change():
    s = Store(":memory:")
    add(s, "up", NOW - 300)
    add(s, "down", NOW - 200)
    add(s, "down", NOW - 100)
    assert s.last_change("dev") == NOW - 200


def test_prune():
    s = Store(":memory:")
    add(s, "up", NOW - 10 * 86400)
    s.add_event(Event("dev", "info", "old", timestamp=NOW - 10 * 86400))
    add(s, "up", NOW)
    s.prune(7, now=NOW)
    assert s.uptime("dev", window_s=30 * 86400, now=NOW) == 100.0
    assert s.events() == []


def _config(port=0):
    return Config(web_host="127.0.0.1", web_port=port,
                  devices=[DeviceConfig(id="dev", name="Device", type="tcp", host="h", group="Audio")])


def test_build_status_shape():
    s = Store(":memory:")
    add(s, "up", NOW - 5)
    s.add_event(Event("dev", "alert", "hello", timestamp=NOW))
    out = build_status(_config(), s, now=NOW)
    d = out["devices"][0]
    assert d["status"] == "up" and d["group"] == "Audio" and d["uptime_24h"] == 100.0
    assert len(d["latency_24h"]) == 48
    assert out["events"][0]["message"] == "hello"


@pytest.fixture
def server():
    s = Store(":memory:")
    add(s, "up", NOW)
    httpd = start_server(_config(), s)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_server_serves_dashboard_and_api(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert "<title>AV Fleet</title>" in html
    assert urllib.request.urlopen(server + "/app.js").headers["Content-Type"].startswith("text/javascript")
    data = json.loads(urllib.request.urlopen(server + "/api/status").read())
    assert data["devices"][0]["id"] == "dev"


@pytest.mark.parametrize("path", ["/../pyproject.toml", "/%2e%2e/server.py", "/server.py", "/nope.html"])
def test_server_rejects_other_paths(server, path):
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(server + path)
    assert err.value.code == 404
