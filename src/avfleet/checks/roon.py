"""Roon Core.

Roon has no lightweight public health endpoint (its extension API is a WebSocket protocol
with discovery and pairing), so "functioning" here means the Core is accepting connections
on its service port. When the monitor runs on the same Mac as the Core, it also reports
which Roon processes are alive and how busy the Core is, which the restart action uses to
avoid killing a database upgrade in progress.
"""

from __future__ import annotations

import subprocess

from ..models import Status
from .base import Check, register, tcp_connect

CORE_PROCESSES = ("RoonServer", "RoonAppliance")


def core_cpu_percent() -> float:
    """Highest CPU % among local Roon Core processes (0 if none). macOS/Linux `ps`."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "%cpu=,comm="], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0.0
    best = 0.0
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and any(p in parts[1] for p in CORE_PROCESSES):
            try:
                best = max(best, float(parts[0]))
            except ValueError:
                continue
    return best


def running_processes() -> list[str]:
    try:
        out = subprocess.run(
            ["pgrep", "-l", "-i", "roon"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.split(None, 1)[1] for line in out.splitlines() if " " in line})


@register("roon")
class RoonCheck(Check):
    """Options: port (default 9330), local (default false: also inspect local processes)."""

    def probe(self):
        port = int(self.opt("port", 9330))
        metrics: dict = {}
        if self.opt("local", False):
            metrics["processes"] = running_processes()
            metrics["core_cpu"] = core_cpu_percent()
        try:
            ms = tcp_connect(self.device.host, port, self.device.timeout_s)
        except OSError as exc:
            detail = f"Core not accepting connections on {port}: {exc.strerror or exc}"
            if metrics.get("core_cpu", 0) >= 15:
                detail += f" (Core busy at {metrics['core_cpu']:.0f}% CPU, possibly updating)"
                metrics["notice"] = "Core busy, likely updating: self-healing waits"
            return self.result(Status.DOWN, detail, metrics=metrics)
        return self.result(Status.UP, f"Core Serving · Port {port}", latency_ms=ms, metrics=metrics)
