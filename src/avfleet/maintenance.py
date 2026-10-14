"""Maintenance windows: tell avfleet "I'm working on this, don't page me and don't fix it".

While a device is in maintenance, it is still checked and its results are still recorded
(so the dashboard stays live), but alerts and self-healing are suspended, and those checks
are left out of uptime. When the window ends, by time or by ``--end``, failure counting
starts fresh, so a device that is still down pages normally after ``fail_threshold`` checks.

Used by the CLI (``avfleet maintain``) and designed to be called by other front ends later.
"""

from __future__ import annotations

import re
import time

from .config import Config
from .models import Event
from .store import Store

_PART = re.compile(r"(\d+)\s*([smhd])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_SECONDS = 7 * 86400


def parse_duration(text: str) -> int:
    """'30m', '2h', '1h30m', '90s', '1d'. A bare number means minutes."""
    text = text.strip().lower()
    if text.isdigit():
        seconds = int(text) * 60
    else:
        parts = _PART.findall(text)
        if not parts or _PART.sub("", text).strip():
            raise ValueError(f"can't read duration {text!r} (try 30m, 2h, 1h30m)")
        seconds = sum(int(n) * _UNITS[u] for n, u in parts)
    if not 0 < seconds <= MAX_SECONDS:
        raise ValueError("duration must be between 1 second and 7 days")
    return seconds


def resolve_devices(config: Config, names: list[str]) -> list[str]:
    known = [d.id for d in config.enabled_devices]
    if names == ["all"]:
        return known
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"unknown device(s): {', '.join(unknown)} (known: {', '.join(known)})")
    return names


def _clock(ts: float) -> str:
    return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0")


def start(store: Store, config: Config, names: list[str], seconds: int, reason: str = "",
          now: float | None = None) -> list[str]:
    now = now or time.time()
    ids = resolve_devices(config, names)
    until = now + seconds
    names_by_id = {d.id: d.name for d in config.devices}
    for dev_id in ids:
        store.start_maintenance(dev_id, until, reason, now=now)
        note = f" ({reason})" if reason else ""
        store.add_event(Event(dev_id, "maintenance",
                              f"{names_by_id[dev_id]}: maintenance until {_clock(until)}{note}", timestamp=now))
    return ids


def end(store: Store, config: Config, names: list[str], now: float | None = None) -> list[str]:
    now = now or time.time()
    ids = resolve_devices(config, names)
    names_by_id = {d.id: d.name for d in config.devices}
    ended = []
    for dev_id in ids:
        if store.end_maintenance(dev_id, now=now):
            ended.append(dev_id)
            store.add_event(Event(dev_id, "maintenance", f"{names_by_id[dev_id]}: maintenance ended", timestamp=now))
    return ended


def describe(store: Store, config: Config, now: float | None = None) -> list[str]:
    now = now or time.time()
    names_by_id = {d.id: d.name for d in config.devices}
    lines = []
    for dev_id, w in store.active_maintenance(now).items():
        mins = max(1, round((w["until"] - now) / 60))
        note = f" - {w['reason']}" if w["reason"] else ""
        lines.append(f"{names_by_id.get(dev_id, dev_id)}: until {_clock(w['until'])} ({mins} min left){note}")
    return lines
