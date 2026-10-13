"""Alerting and self-healing rules, driven by scripted results and a fake clock."""

import pytest

from avfleet.actions.base import Action, register_action
from avfleet.config import Config, DeviceConfig, RemediationConfig
from avfleet.engine import Monitor, RemediationPolicy
from avfleet.models import ActionResult, CheckResult, Status
from avfleet.store import Store

RUNS: list[str] = []
BLOCK = {"value": False}


@register_action("test_fix")
class TestFix(Action):
    __test__ = False

    def should_run(self):
        return (False, "busy") if BLOCK["value"] else (True, "")

    def execute(self):
        RUNS.append(self.device.id)
        return ActionResult(self.name, True, "fixed")


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, title, message, priority=0):
        self.sent.append(title)
        return True


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def tick(self, s=60):
        self.t += s


@pytest.fixture(autouse=True)
def _reset():
    RUNS.clear()
    BLOCK["value"] = False


def make(remediation=None, threshold=3):
    dev = DeviceConfig(id="roon", name="Roon", type="tcp", host="127.0.0.1", options={"port": 1},
                       remediation=remediation)
    cfg = Config(fail_threshold=threshold, devices=[dev])
    store, rec, clock = Store(":memory:"), Recorder(), Clock()
    return Monitor(cfg, store, [rec], clock=clock), store, rec, clock


def feed(mon, clock, *statuses):
    for s in statuses:
        mon.process(CheckResult("roon", Status(s), detail="scripted"))
        clock.tick()


def test_alert_only_after_threshold_and_once():
    mon, _, rec, clock = make()
    feed(mon, clock, "down", "down")
    assert rec.sent == []
    feed(mon, clock, "down", "down", "down")
    assert rec.sent == ["Roon is DOWN"]


def test_single_blip_never_alerts():
    mon, _, rec, clock = make()
    feed(mon, clock, "down", "up", "down", "up", "down", "down", "up")
    assert rec.sent == []


def test_recovery_alert_after_down():
    mon, store, rec, clock = make()
    feed(mon, clock, "down", "down", "down", "up")
    assert rec.sent == ["Roon is DOWN", "Roon recovered"]
    kinds = [e["kind"] for e in store.events()]
    assert "state" in kinds and kinds.count("alert") == 2


def test_degraded_is_logged_not_paged():
    mon, store, rec, clock = make()
    feed(mon, clock, "up", "degraded", "degraded", "degraded", "up")
    assert rec.sent == []
    assert sum(e["kind"] == "state" for e in store.events()) == 2


def test_remediation_fires_after_n_failures_then_resets_counter():
    rem = RemediationConfig(action="test_fix", after_failures=3, cooldown_s=600, max_per_hour=3)
    mon, store, rec, clock = make(rem)
    feed(mon, clock, "down", "down")
    assert RUNS == []
    feed(mon, clock, "down")
    assert RUNS == ["roon"]
    assert "Roon: test_fix" in rec.sent
    feed(mon, clock, "down", "down")  # counter was reset: not yet
    assert RUNS == ["roon"]


def test_remediation_respects_cooldown():
    rem = RemediationConfig(action="test_fix", after_failures=1, cooldown_s=600, max_per_hour=10)
    mon, _, _, clock = make(rem)
    feed(mon, clock, "down")          # run at t0
    feed(mon, clock, *["down"] * 5)   # t0+60..t0+300: inside cooldown
    assert len(RUNS) == 1
    clock.tick(600)
    feed(mon, clock, "down")
    assert len(RUNS) == 2


def test_remediation_hourly_limit_alerts_once():
    rem = RemediationConfig(action="test_fix", after_failures=1, cooldown_s=0, max_per_hour=2)
    mon, _, rec, clock = make(rem)
    feed(mon, clock, *["down"] * 6)
    assert len(RUNS) == 2
    assert rec.sent.count("Roon: self-healing paused") == 1
    clock.tick(3700)  # an hour later the budget refills
    feed(mon, clock, "down")
    assert len(RUNS) == 3


def test_blocked_precondition_does_not_consume_budget():
    rem = RemediationConfig(action="test_fix", after_failures=1, cooldown_s=0, max_per_hour=1)
    mon, store, _, clock = make(rem)
    BLOCK["value"] = True
    feed(mon, clock, "down", "down")
    assert RUNS == []
    assert any("skipped: busy" in e["message"] for e in store.events())
    BLOCK["value"] = False
    feed(mon, clock, "down")
    assert RUNS == ["roon"]


def test_dry_run_records_but_does_not_execute():
    rem = RemediationConfig(action="test_fix", after_failures=1)
    mon, store, _, clock = make(rem)
    mon.dry_run = True
    feed(mon, clock, "down")
    assert RUNS == []
    assert any("dry run" in e["message"] for e in store.events())


def test_policy_decisions():
    p = RemediationPolicy(RemediationConfig(action="x", after_failures=2, cooldown_s=100, max_per_hour=2))
    assert p.decide(1, 0) == "wait"
    assert p.decide(2, 0) == "run"
    p.record(0)
    assert p.decide(2, 50) == "cooldown"
    p.record(150)
    assert p.decide(2, 300) == "limit"
    assert p.decide(2, 3700) == "run"


def test_unknown_check_type_fails_fast():
    dev = DeviceConfig(id="x", name="x", type="nope", host="h")
    with pytest.raises(ValueError, match="unknown type"):
        Monitor(Config(devices=[dev]), Store(":memory:"), [])


def test_run_once_against_closed_port(closed_port):
    dev = DeviceConfig(id="roon", name="Roon", type="tcp", host="127.0.0.1", options={"port": closed_port},
                       timeout_s=1)
    store = Store(":memory:")
    mon = Monitor(Config(devices=[dev]), store, [])
    [result] = mon.run_once()
    assert result.status is Status.DOWN
    assert store.latest("roon")["status"] == "down"
