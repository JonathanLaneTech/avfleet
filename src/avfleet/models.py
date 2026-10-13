"""Core data types shared by checks, the engine, storage and the dashboard."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """Health of a device, from best to worst.

    UP        the device's own service answered and reports itself healthy
    DEGRADED  reachable, but something is off (a switch port down, a component in standby fault)
    DOWN      the service did not answer, or answered with an error
    UNKNOWN   the check itself could not run (bad config, missing credentials)
    """

    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"

    @property
    def is_failure(self) -> bool:
        return self is Status.DOWN


@dataclass(slots=True)
class CheckResult:
    device_id: str
    status: Status
    latency_ms: float | None = None
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass(slots=True)
class Event:
    """Something worth showing in the event log: a state change, an alert, a remediation."""

    device_id: str
    kind: str  # "state", "action", "alert", "info"
    message: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ActionResult:
    action: str
    ok: bool
    message: str
