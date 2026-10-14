"""Manual reboots: the one place the dashboard buttons and Claude's reboot tool both go through.

Guard rails, in order: the device must have a ``[devices.reboot]`` table, the last reboot must
be older than its cooldown, and the action's own precondition (e.g. Roon not mid-upgrade) must
agree. Every attempt, successful or not, lands in the event log with who asked for it.
"""

from __future__ import annotations

import time

from .actions import build_action
from .config import Config, DeviceConfig
from .models import ActionResult, Event
from .store import Store


class RebootError(ValueError):
    pass


def rebootable(config: Config) -> list[DeviceConfig]:
    return [d for d in config.enabled_devices if d.reboot]


def run(store: Store, config: Config, device_id: str, via: str, now: float | None = None,
        dry_run: bool = False) -> ActionResult:
    now = now or time.time()
    dev = next((d for d in config.enabled_devices if d.id == device_id), None)
    if dev is None:
        raise RebootError(f"unknown device '{device_id}'")
    if dev.reboot is None:
        names = ", ".join(d.id for d in rebootable(config)) or "none configured"
        raise RebootError(f"{dev.name} has no reboot action (rebootable: {names})")
    recent = store.events_for(dev.id, kind="reboot", limit=1, since=now - dev.reboot.cooldown_s)
    if recent and "requested" in recent[0]["message"]:
        wait = int((dev.reboot.cooldown_s - (now - recent[0]["ts"])) // 60) + 1
        raise RebootError(f"{dev.name} was rebooted moments ago; try again in about {wait} min")
    action = build_action(dev.reboot.action, dev, dev.reboot.options, dry_run=dry_run)
    outcome = action.run()
    verb = "requested" if outcome.ok else "failed"
    store.add_event(Event(dev.id, "reboot", f"{dev.name}: {dev.reboot.label.lower()} {verb} via {via}"
                          f" -> {outcome.message}", timestamp=now))
    return outcome
