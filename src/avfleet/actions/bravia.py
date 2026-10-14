"""Reboot a Sony Bravia TV through its REST API (system service, ``requestReboot``).

Uses the same pre-shared key as the Bravia health check (the device's ``psk`` option),
so the TV needs IP control with "Normal and Pre-Shared Key" authentication enabled.
"""

from __future__ import annotations

import json

from ..checks.base import http_request
from ..models import ActionResult
from .base import Action, register_action


@register_action("bravia_reboot")
class BraviaReboot(Action):
    def execute(self):
        psk = self.options.get("psk") or self.device.options.get("psk")
        headers = {"Content-Type": "application/json"}
        if psk:
            headers["X-Auth-PSK"] = str(psk)
        body = json.dumps({"method": "requestReboot", "id": 10, "params": [], "version": "1.0"}).encode()
        resp = http_request(f"http://{self.device.host}/sony/system", self.device.timeout_s,
                            method="POST", data=body, headers=headers)
        if resp.status != 200:
            return ActionResult(self.name, False, f"TV answered HTTP {resp.status}")
        data = resp.json()
        if "error" in data:
            code = data["error"][0] if data["error"] else "?"
            hint = " (check the TV's pre-shared key)" if code in (401, 403) else ""
            return ActionResult(self.name, False, f"TV refused: error {code}{hint}")
        return ActionResult(self.name, True, "reboot requested; the TV will be back in about a minute")
