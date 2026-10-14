"""Maintenance windows: storage, engine behaviour, CLI, notices, dashboard data."""

import sqlite3

import pytest

from avfleet import maintenance
from avfleet.checks.lutron import LutronDevice
from avfleet.checks.lutron import summarize as lutron_summary
from avfleet.checks.unifi import summarize_device
from avfleet.cli import main
from avfleet.config import Config, DeviceConfig, RemediationConfig
from avfleet.engine import Monitor
from avfleet.models import CheckResult, Status
from avfleet.store import Store
from avfleet.web import build_status

T0 = 1_000_000.0


@pytest.mark.parametrize("text,seconds", [("30m", 1800), ("2h", 7200), ("1h30m", 5400), ("90s", 90),
                                          ("1d", 86400), ("45", 2700), (" 10M ", 600)])
def test_parse_duration(text, seconds):
    assert maintenance.parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "soon", "30x", "0m", "8d", "1h junk"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        maintenance.parse_duration(bad)


def test_store_windows_start_end_replace():
    s = Store(":memory:")
    s.start_maintenance("lutron", T0 + 600, "transfer", now=T0)
    assert s.active_maintenance(T0 + 1)["lutron"]["reason"] == "transfer"
    assert s.active_maintenance(T0 + 601) == {}
    s.start_maintenance("lutron", T0 + 1200, "longer", now=T0 + 100)  # replaces, doesn't stack
    assert s.active_maintenance(T0 + 700)["lutron"]["until"] == T0 + 1200
    assert s.end_maintenance("lutron", now=T0 + 800) is True
    assert s.active_maintenance(T0 + 801) == {}
    assert s.end_maintenance("lutron", now=T0 + 900) is False


def test_old_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE results (ts REAL NOT NULL, device_id TEXT NOT NULL, status TEXT NOT NULL, "
                "latency_ms REAL, detail TEXT, metrics TEXT)")
    con.execute("INSERT INTO results VALUES (1.0, 'roon', 'up', 3.0, '', '{}')")
    con.commit()
    con.close()
    s = Store(db)
    s.add_result(CheckResult("roon", Status.DOWN, timestamp=2.0), maintenance=True)
    assert s.uptime("roon", window_s=10, now=5.0) == 100.0  # old row counts, maintenance row doesn't


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, title, message, priority=0):
        self.sent.append(title)
        return True


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def tick(self, s=60):
        self.t += s


def make(remediation=None):
    dev = DeviceConfig(id="roon", name="Roon", type="tcp", host="127.0.0.1", options={"port": 1},
                       remediation=remediation)
    cfg = Config(fail_threshold=3, devices=[dev])
    store, rec, clock = Store(":memory:"), Recorder(), Clock()
    return Monitor(cfg, store, [rec], clock=clock), store, rec, clock, cfg


def feed(mon, clock, *statuses):
    for st in statuses:
        mon.process(CheckResult("roon", Status(st), detail="scripted"))
        clock.tick()


def test_no_alerts_or_self_healing_during_maintenance_then_fresh_count():
    runs = []
    from avfleet.actions.base import Action, register_action

    @register_action("maint_test_fix")
    class Fix(Action):
        def execute(self):
            runs.append(1)
            from avfleet.models import ActionResult
            return ActionResult(self.name, True, "fixed")

    mon, store, rec, clock, cfg = make(RemediationConfig(action="maint_test_fix", after_failures=3, cooldown_s=0))
    maintenance.start(store, cfg, ["roon"], 300, "rewiring", now=clock())
    feed(mon, clock, *["down"] * 5)  # five minutes down, inside the window
    assert rec.sent == [] and runs == []
    assert store.uptime("roon", now=clock()) is None  # maintenance checks don't count at all
    feed(mon, clock, "down", "down")  # window over: count starts fresh, not at 7
    assert rec.sent == []
    assert any("maintenance window ended" in e["message"] for e in store.events())
    feed(mon, clock, "down")  # third failure after the window: page and fix, as normal
    assert rec.sent[0] == "Roon is DOWN" and runs == [1]


def test_ending_early_resumes_and_is_logged_once():
    mon, store, rec, clock, cfg = make()
    maintenance.start(store, cfg, ["roon"], 3600, now=clock())
    feed(mon, clock, "down", "down")
    assert maintenance.end(store, cfg, ["roon"], now=clock()) == ["roon"]
    feed(mon, clock, "down", "down", "down")
    assert rec.sent == ["Roon is DOWN"]
    msgs = [e["message"] for e in store.events() if e["kind"] == "maintenance"]
    assert msgs.count("Roon: maintenance ended") == 1
    assert not any("window ended" in m for m in msgs)


def test_unknown_device_rejected():
    _, store, _, clock, cfg = make()
    with pytest.raises(ValueError, match="unknown device"):
        maintenance.start(store, cfg, ["projector"], 60, now=clock())
    assert maintenance.start(store, cfg, ["all"], 60, now=clock()) == ["roon"]


def test_dashboard_reports_window():
    _, store, _, clock, cfg = make()
    maintenance.start(store, cfg, ["roon"], 1800, "firmware", now=clock())
    dev = build_status(cfg, store, now=clock() + 1)["devices"][0]
    assert dev["maintenance"]["reason"] == "firmware" and dev["maintenance"]["until"] == clock() + 1800


def test_cli_maintain_round_trip(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[monitor]\ndb_path = "{tmp_path / "a.db"}"\n'
                   '[[devices]]\nid = "lutron"\nname = "Lutron"\ntype = "tcp"\nhost = "h"\n')
    assert main(["maintain", "-c", str(cfg), "lutron", "30m", "--reason", "transfer"]) == 0
    assert "In maintenance for 30m: lutron" in capsys.readouterr().out
    assert main(["maintain", "-c", str(cfg), "--list"]) == 0
    assert "Lutron: until" in capsys.readouterr().out
    assert main(["maintain", "-c", str(cfg), "--end", "lutron"]) == 0
    assert "Ended maintenance for: lutron" in capsys.readouterr().out
    assert main(["maintain", "-c", str(cfg), "lutron", "forever"]) == 2
    assert main(["maintain", "-c", str(cfg), "nope", "5m"]) == 2


def test_device_notices():
    _, _, m = summarize_device({"mac": "x", "state": 4})
    assert m["notice"] == "Firmware updating"
    _, _, m = summarize_device({"mac": "x", "state": 1})
    assert "notice" not in m
    proc = LutronDevice("/device/1", "proc", "HWQSProcessor", transfer="InProgress")
    status, _, m = lutron_summary({"processor": proc, "devices": []})
    assert status is Status.DEGRADED and m["notice"] == "Programming transfer inprogress"
