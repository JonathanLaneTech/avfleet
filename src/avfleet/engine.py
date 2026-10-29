"""The monitor loop: run checks, record results, decide on alerts and self-healing.

Alerting rules (per device):
  * A device is declared DOWN after ``fail_threshold`` consecutive failed checks, so one
    dropped packet never pages anyone. One alert per outage.
  * Recovery is announced on the first healthy check after a DOWN alert.
  * DEGRADED and UNKNOWN are logged as events, not paged.

Maintenance windows (``avfleet maintain``): the device is still checked and recorded, but
nothing pages and nothing self-heals, and those checks don't count against uptime.

Self-healing rules (per device with a [devices.remediation] table):
  * Fires after ``after_failures`` consecutive failures, if the action's own precondition
    agrees (e.g. Roon is not mid-upgrade).
  * At most once per ``cooldown_s``, and at most ``max_per_hour`` times in a rolling hour.
    When the hourly limit is hit, it stops and alerts a human once.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .actions import build_action
from .checks import build_check
from .config import Config, DeviceConfig, RemediationConfig
from .models import CheckResult, Event, Status
from .notifiers import Notifier
from .store import Store

log = logging.getLogger("avfleet.engine")


@dataclass
class RemediationPolicy:
    cfg: RemediationConfig
    history: deque[float] = field(default_factory=deque)
    gave_up_alerted: bool = False

    def decide(self, consecutive_failures: int, now: float) -> str:
        """Return 'wait', 'cooldown', 'limit' or 'run'."""
        if consecutive_failures < self.cfg.after_failures:
            return "wait"
        while self.history and now - self.history[0] > 3600:
            self.history.popleft()
        if self.history and now - self.history[-1] < self.cfg.cooldown_s:
            return "cooldown"
        if len(self.history) >= self.cfg.max_per_hour:
            return "limit"
        return "run"

    def record(self, now: float) -> None:
        self.history.append(now)
        self.gave_up_alerted = False


@dataclass
class DeviceState:
    device: DeviceConfig
    consecutive_failures: int = 0
    alerted_down: bool = False
    last_status: Status | None = None
    policy: RemediationPolicy | None = None
    in_maintenance: bool = False


class Monitor:
    def __init__(self, config: Config, store: Store, notifiers: list[Notifier], dry_run: bool = False,
                 clock=time.time):
        self.config = config
        self.store = store
        self.notifiers = notifiers
        self.dry_run = dry_run
        self.clock = clock
        self.states: dict[str, DeviceState] = {}
        for dev in config.enabled_devices:
            build_check(dev)  # fail fast on unknown types
            policy = RemediationPolicy(dev.remediation) if dev.remediation else None
            self.states[dev.id] = DeviceState(dev, policy=policy)

    # --- public -----------------------------------------------------------------------

    def run_once(self) -> list[CheckResult]:
        devices = [s.device for s in self.states.values()]
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(devices)))) as pool:
            results = list(pool.map(lambda d: build_check(d).run(), devices))
        for result in results:
            self.process(result)
        return results

    def run_forever(self) -> None:
        last_prune = 0.0
        while True:
            started = self.clock()
            try:
                self.run_once()
                if started - last_prune > 3600:
                    self.store.prune(self.config.history_days, now=started)
                    last_prune = started
            except Exception:  # noqa: BLE001 - keep the loop alive, log the bug
                log.exception("monitor pass failed")
            time.sleep(max(1.0, self.config.interval_s - (self.clock() - started)))

    # --- core logic (pure enough to unit test with a fake clock) -------------------------

    def process(self, result: CheckResult) -> None:
        state = self.states[result.device_id]
        dev = state.device
        now = self.clock()
        result.timestamp = now
        in_maint = dev.id in self.store.active_maintenance(now)
        self.store.add_result(result, maintenance=in_maint)
        self._track_maintenance(state, in_maint)

        if state.last_status is not None and result.status != state.last_status:
            self._event(dev.id, "state", f"{dev.name}: {state.last_status.value} -> {result.status.value}"
                        + (f" ({result.detail})" if result.detail else ""))
        elif state.last_status is None and result.status is not Status.UP:
            self._event(dev.id, "info", f"{dev.name}: first check {result.status.value}"
                        + (f" ({result.detail})" if result.detail else ""))
        state.last_status = result.status

        if in_maint:
            return  # checked and recorded, but no paging and no self-healing while someone is working on it

        if result.status.is_failure:
            state.consecutive_failures += 1
            threshold = dev.fail_threshold or self.config.fail_threshold
            if state.consecutive_failures >= threshold and not state.alerted_down:
                state.alerted_down = True
                if dev.alerts:
                    self._alert(dev, f"{dev.name} is DOWN", result.detail or "no response", priority=1)
            if state.policy:
                self._maybe_remediate(state, now)
        elif result.status in (Status.UP, Status.DEGRADED):
            if state.alerted_down and self.config.notify_recovery and dev.alerts:
                self._alert(dev, f"{dev.name} recovered", result.detail or result.status.value)
            state.alerted_down = False
            state.consecutive_failures = 0

    def _track_maintenance(self, state: DeviceState, in_maint: bool) -> None:
        if in_maint == state.in_maintenance:
            return
        state.in_maintenance = in_maint
        # Either edge starts the failure count fresh: nothing during the window counts toward a page.
        state.consecutive_failures = 0
        state.alerted_down = False
        if not in_maint:
            last = self.store.last_maintenance(state.device.id)
            if last and not last["ended_early"]:  # an early end was already logged by whoever ended it
                self._event(state.device.id, "maintenance", f"{state.device.name}: maintenance window ended")

    def _maybe_remediate(self, state: DeviceState, now: float) -> None:
        policy = state.policy
        assert policy is not None
        decision = policy.decide(state.consecutive_failures, now)
        dev = state.device
        if decision == "limit":
            if not policy.gave_up_alerted:
                policy.gave_up_alerted = True
                self._alert(dev, f"{dev.name}: self-healing paused",
                            f"{policy.cfg.action} ran {policy.cfg.max_per_hour}x in an hour; needs a human",
                            priority=1)
            return
        if decision != "run":
            return
        action = build_action(policy.cfg.action, dev, policy.cfg.options, dry_run=self.dry_run)
        outcome = action.run()
        if outcome.message.startswith("skipped"):
            self._event(dev.id, "action", f"{dev.name}: {action.name} {outcome.message}")
            return
        policy.record(now)
        state.consecutive_failures = 0  # give the fix time to work before judging again
        self._event(dev.id, "action", f"{dev.name}: {action.name} -> {outcome.message}")
        self._alert(dev, f"{dev.name}: {action.name}", outcome.message, priority=0)

    # --- helpers --------------------------------------------------------------------------

    def _event(self, device_id: str, kind: str, message: str) -> None:
        log.info("%s", message)
        self.store.add_event(Event(device_id, kind, message, timestamp=self.clock()))

    def _alert(self, dev: DeviceConfig, title: str, message: str, priority: int = 0) -> None:
        self._event(dev.id, "alert", f"{title}: {message}")
        for n in self.notifiers:
            try:
                n.send(title, message, priority)
            except Exception:  # noqa: BLE001
                log.exception("notifier %s failed", type(n).__name__)
