"""Yamaha MusicCast receivers via Yamaha Extended Control (YXC), e.g. the R-N2000A.

GET /YamahaExtendedControl/v1/<zone>/getStatus returns JSON with response_code 0 on success,
plus power, input, volume and mute. Standby is healthy: the network stack is up and the
unit will answer control commands.
"""

from __future__ import annotations

from ..models import Status
from .base import Check, http_request, register


@register("yamaha_yxc")
class YamahaYxcCheck(Check):
    """Options: zone (default "main")."""

    def probe(self):
        zone = self.opt("zone", "main")
        url = f"http://{self.device.host}/YamahaExtendedControl/v1/{zone}/getStatus"
        resp = http_request(url, self.device.timeout_s)
        if resp.status != 200:
            return self.result(Status.DOWN, f"YXC HTTP {resp.status}", latency_ms=resp.latency_ms)
        data = resp.json()
        code = data.get("response_code")
        metrics = {k: data[k] for k in ("power", "input", "volume", "max_volume", "mute") if k in data}
        if code != 0:
            return self.result(
                Status.DEGRADED, f"YXC response_code {code}", latency_ms=resp.latency_ms, metrics=metrics
            )
        power = data.get("power", "unknown")
        detail = f"{power}" + (f", input {data['input']}" if power == "on" and "input" in data else "")
        return self.result(Status.UP, detail, latency_ms=resp.latency_ms, metrics=metrics)
