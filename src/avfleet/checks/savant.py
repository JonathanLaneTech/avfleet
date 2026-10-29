"""Savant host (Smart Host, Pro Host and other RacePoint OS hosts).

Two layers, on two schedules:

* Every check: is the host answering on SSH (port 22)? Cheap, and catches a dead box fast.
* Every ``info_interval_s`` (default 300 s): log in with a key and read what Savant's own
  System Monitor shows (name, unique ID, software version, OS version, model) plus whether
  the Savant system itself is running. A host can be powered and answering while its
  configuration has stopped; then nothing in the house responds. That is the case the
  port check alone cannot see.

Everything run on the host is read-only. Savant's tools are called by full path because an
SSH command does not get the interactive shell's PATH.

Setup: an SSH key for the host's RPM account (``ssh-copy-id``), referenced by ``ssh_key``.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from ..models import Status
from .base import Check, register, tcp_connect

# The processes that mean "the Savant system is running", not just "the box is on".
CORE_PROCESSES = ("rpmStateCenter", "startupManager", "rpmMonitord")

SEP = "=====avfleet====="
INFO_SCRIPT = "; ".join([
    "/usr/local/bin/racepointosver 2>/dev/null | head -12",
    f"echo {SEP}", "uname -r",
    f"echo {SEP}", "hostname",
    f"echo {SEP}", "cat /proc/device-tree/model 2>/dev/null",
    f"echo {SEP}", "ps aux",
])

# device id -> (time read, parsed info). Checks are rebuilt every cycle, so the cache lives here.
_CACHE: dict[str, tuple[float, dict]] = {}


def ssh_argv(user: str, host: str, key: str | None, timeout: float) -> list[str]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(max(timeout, 3))}"]
    if key:
        argv += ["-i", os.path.expanduser(key)]
    return argv + [f"{user}@{host}"]


def parse_info(text: str) -> dict:
    """Turn the INFO_SCRIPT output into the fields System Monitor shows, plus process health."""
    parts = text.split(SEP)
    parts += [""] * (5 - len(parts))
    osver, kernel, hostname, model, ps = (p.strip() for p in parts[:5])

    info: dict = {}
    m = re.search(r"Release version\s*:\s*(\S+)", osver)
    if m:
        info["version"] = m.group(1)
    if kernel:
        info["os_version"] = kernel.split("+", 1)[0]
    if hostname:
        info["unique_id"] = hostname.removeprefix("sav-").upper()
    if model:
        info["model"] = model.strip("\x00").strip()

    running = set()
    for line in ps.splitlines():
        for proc in CORE_PROCESSES:
            if re.search(rf"/{proc}(\s|$)", line):
                running.add(proc)
        m = re.search(r"/avc (.+?)#RacePointMedia_host", line)
        if m:
            info["name"] = m.group(1).strip()
    info["processes"] = {p: p in running for p in CORE_PROCESSES}
    return info


@register("savant_host")
class SavantHostCheck(Check):
    """Options: ssh_key (path), user (default "RPM"), port (default 22),
    info_interval_s (default 300)."""

    def run_ssh(self, script: str, timeout: float = 20) -> subprocess.CompletedProcess:
        argv = ssh_argv(self.opt("user", "RPM"), self.device.host, self.opt("ssh_key"),
                        self.device.timeout_s)
        return subprocess.run(argv + [script], capture_output=True, text=True,
                              timeout=timeout, check=False)

    def read_info(self) -> dict:
        """Fresh info from the host if the cached copy is older than info_interval_s."""
        now = time.time()
        cached = _CACHE.get(self.device.id)
        if cached and now - cached[0] < float(self.opt("info_interval_s", 300)):
            return cached[1]
        try:
            proc = self.run_ssh(INFO_SCRIPT)
        except (OSError, subprocess.SubprocessError) as exc:
            info = {"error": f"SSH: {exc}"}
        else:
            if proc.returncode != 0:
                err = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
                info = {"error": f"SSH login failed: {err}"}
            else:
                info = parse_info(proc.stdout)
        _CACHE[self.device.id] = (now, info)
        return info

    def probe(self):
        port = int(self.opt("port", 22))
        try:
            ms = tcp_connect(self.device.host, port, self.device.timeout_s)
        except OSError as exc:
            return self.result(Status.DOWN, f"port {port}: {exc.strerror or exc}")

        info = self.read_info()
        if "error" in info:
            # The box answers, but we cannot see inside it: not down, not healthy.
            return self.result(Status.DEGRADED, f"host answering; {info['error']}", latency_ms=ms,
                               metrics=info)

        stopped = [p for p, ok in info["processes"].items() if not ok]
        facts = " · ".join(x for x in (
            f"Savant {info['version']}" if info.get("version") else "",
            f"OS {info['os_version']}" if info.get("os_version") else "",
            info.get("model", ""),
        ) if x)
        if stopped:
            return self.result(Status.DOWN, f"Savant services stopped: {', '.join(stopped)}"
                               + (f" ({facts})" if facts else ""), latency_ms=ms, metrics=info)
        return self.result(Status.UP, f"{facts} · system running" if facts else "system running",
                           latency_ms=ms, metrics=info)
