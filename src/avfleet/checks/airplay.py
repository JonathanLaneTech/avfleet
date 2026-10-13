"""Apple TV and HomePod via the AirPlay service (TCP 7000).

GET /info on an AirPlay receiver returns a binary property list with the model, name and
OS build. Some devices refuse /info without pairing (HTTP 403); an answer of any kind still
proves the AirPlay service, which is what users actually depend on, is alive.
"""

from __future__ import annotations

import plistlib

from ..models import Status
from .base import Check, http_request, register


class AirPlayCheck(Check):
    """Options: port (default 7000)."""

    def probe(self):
        port = int(self.opt("port", 7000))
        resp = http_request(f"http://{self.device.host}:{port}/info", self.device.timeout_s)
        if resp.status == 200:
            metrics = {}
            try:
                info = plistlib.loads(resp.body)
                for src, dst in (("model", "model"), ("name", "name"), ("osBuildVersion", "os_build"),
                                 ("sourceVersion", "airplay_version")):
                    if src in info:
                        metrics[dst] = str(info[src])
            except (plistlib.InvalidFileException, ValueError, TypeError):
                pass
            detail = "AirPlay ready" + (f" ({metrics['model']})" if "model" in metrics else "")
            return self.result(Status.UP, detail, latency_ms=resp.latency_ms, metrics=metrics)
        if resp.status in (401, 403, 470):
            return self.result(Status.UP, f"AirPlay answering (HTTP {resp.status})", latency_ms=resp.latency_ms)
        return self.result(Status.DOWN, f"AirPlay HTTP {resp.status}", latency_ms=resp.latency_ms)


@register("apple_tv")
class AppleTvCheck(AirPlayCheck):
    pass


@register("homepod")
class HomePodCheck(AirPlayCheck):
    pass
