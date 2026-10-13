"""Action plug-ins: things the monitor may do on its own to fix a device.

An action has a precondition (``should_run``: is it safe right now?) and an effect
(``execute``). When and how often actions fire is decided by RemediationPolicy in the
engine, never by the action itself, so every action gets debounce, cooldown and a rate
limit for free. ``dry_run`` logs what would happen without doing it.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from ..config import DeviceConfig
from ..models import ActionResult

_ACTIONS: dict[str, type[Action]] = {}


def register_action(name: str) -> Callable[[type[Action]], type[Action]]:
    def deco(cls: type[Action]) -> type[Action]:
        cls.name = name
        _ACTIONS[name] = cls
        return cls

    return deco


def build_action(name: str, device: DeviceConfig, options: dict[str, Any], dry_run: bool = False) -> Action:
    try:
        cls = _ACTIONS[name]
    except KeyError:
        raise ValueError(f"unknown action '{name}' (known: {', '.join(sorted(_ACTIONS))})") from None
    return cls(device, options, dry_run)


class Runner:
    """Runs shell steps locally, or on another host over SSH (key-based, BatchMode)."""

    def __init__(self, ssh_host: str | None = None):
        self.ssh_host = ssh_host

    def __call__(self, argv: Sequence[str], timeout: float = 30) -> subprocess.CompletedProcess:
        if self.ssh_host:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.ssh_host,
                    " ".join(shlex.quote(a) for a in argv)]
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)


class Action:
    name: ClassVar[str] = ""

    def __init__(self, device: DeviceConfig, options: dict[str, Any], dry_run: bool = False):
        self.device = device
        self.options = options
        self.dry_run = dry_run
        self.run_cmd = Runner(options.get("ssh_host"))

    def should_run(self) -> tuple[bool, str]:
        """Return (ok, reason). Override to block an action that would do harm right now."""
        return True, ""

    def execute(self) -> ActionResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self) -> ActionResult:
        ok, reason = self.should_run()
        if not ok:
            return ActionResult(self.name, False, f"skipped: {reason}")
        if self.dry_run:
            return ActionResult(self.name, True, "dry run: would execute")
        try:
            return self.execute()
        except Exception as exc:  # noqa: BLE001
            return ActionResult(self.name, False, f"{type(exc).__name__}: {exc}")
