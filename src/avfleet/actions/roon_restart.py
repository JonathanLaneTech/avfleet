"""Restart the Roon app (Core + UI) on macOS. This is the original bash watchdog, as a plug-in.

Sequence: ask Roon to quit politely, wait, force-kill anything left (Roon, RoonServer,
RoonAppliance, RAATServer), relaunch. Blocked while the Core is busy, because a Core using
real CPU but not yet serving is usually mid database upgrade after an update.
"""

from __future__ import annotations

import time

from ..checks.roon import core_cpu_percent
from ..models import ActionResult
from .base import Action, register_action

KILL = ["killall", "Roon", "RoonServer", "RoonAppliance", "RAATServer"]


@register_action("roon_restart")
class RoonRestart(Action):
    """Options: busy_cpu (default 15), app (default "Roon"), quit_wait_s (default 8), ssh_host."""

    def should_run(self):
        if self.options.get("ssh_host"):
            return True, ""  # remote CPU not inspected; rely on the policy's cooldown
        threshold = float(self.options.get("busy_cpu", 15))
        cpu = core_cpu_percent()
        if cpu >= threshold:
            return False, f"Core busy at {cpu:.0f}% CPU (likely updating)"
        return True, ""

    def execute(self):
        app = self.options.get("app", "Roon")
        self.run_cmd(["osascript", "-e", "with timeout of 15 seconds", "-e",
                      f'tell application "{app}" to quit', "-e", "end timeout"], timeout=30)
        time.sleep(float(self.options.get("quit_wait_s", 8)))
        self.run_cmd(KILL, timeout=15)
        time.sleep(3)
        launched = self.run_cmd(["open", "-a", app], timeout=15)
        if launched.returncode != 0:
            return ActionResult(self.name, False, f"relaunch failed: {launched.stderr.strip()}")
        return ActionResult(self.name, True, f"restarted {app}")
