"""Reboot a Savant host over SSH, using the same key as the savant_host check.

Savant grants the RPM account password-free ``sudo /sbin/reboot`` for its own tools. The
command checks that permission first, then schedules the reboot two seconds out so the SSH
session can close cleanly instead of being cut off mid-reply.
"""

from __future__ import annotations

import subprocess

from ..checks.savant import ssh_argv
from ..models import ActionResult
from .base import Action, register_action

REBOOT_SCRIPT = (
    "sudo -n -l /sbin/reboot >/dev/null 2>&1 || { echo no-permission; exit 3; }; "
    'nohup sh -c "sleep 2; sudo -n /sbin/reboot" >/dev/null 2>&1 </dev/null & echo scheduled'
)


@register_action("savant_reboot")
class SavantReboot(Action):
    def execute(self):
        opts = {**self.device.options, **self.options}
        argv = ssh_argv(opts.get("user", "RPM"), self.device.host, opts.get("ssh_key"),
                        self.device.timeout_s)
        proc = subprocess.run(argv + [REBOOT_SCRIPT], capture_output=True, text=True,
                              timeout=30, check=False)
        if "scheduled" in proc.stdout:
            return ActionResult(self.name, True, "reboot requested; the host will be back in a few minutes")
        if "no-permission" in proc.stdout:
            return ActionResult(self.name, False, "the RPM account is not allowed to reboot this host")
        err = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        return ActionResult(self.name, False, f"SSH failed: {err}")
