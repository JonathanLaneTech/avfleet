"""Televisions. Every brand speaks something different, so the check takes a ``protocol``.

protocol = "sony_bravia"  Sony Bravia REST API: POST /sony/system getPowerStatus (pre-shared key)
protocol = "samsung"      Samsung Tizen: GET :8001/api/v2/ device info (PowerState on newer models)
protocol = "tcp"          Any other brand: control port answers (e.g. LG webOS 3000/3001)
"""

from __future__ import annotations

import json

from ..models import Status
from .base import Check, http_request, register, tcp_connect


@register("tv")
class TvCheck(Check):
    """Options: protocol, port, psk (Sony; use ${ENV} reference)."""

    def probe(self):
        protocol = self.opt("protocol", "tcp")
        if protocol == "sony_bravia":
            return self._sony()
        if protocol == "samsung":
            return self._samsung()
        if protocol == "tcp":
            port = self.opt("port")
            if not port:
                return self.result(Status.UNKNOWN, "protocol 'tcp' needs a 'port'")
            ms = tcp_connect(self.device.host, int(port), self.device.timeout_s)
            return self.result(Status.UP, f"control port {port} open", latency_ms=ms)
        return self.result(Status.UNKNOWN, f"unknown TV protocol '{protocol}'")

    def _sony(self):
        body = json.dumps({"method": "getPowerStatus", "id": 50, "params": [], "version": "1.0"}).encode()
        headers = {"Content-Type": "application/json"}
        if self.opt("psk"):
            headers["X-Auth-PSK"] = str(self.opt("psk"))
        resp = http_request(f"http://{self.device.host}/sony/system", self.device.timeout_s,
                            method="POST", data=body, headers=headers)
        if resp.status != 200:
            return self.result(Status.DOWN, f"Bravia HTTP {resp.status}", latency_ms=resp.latency_ms)
        data = resp.json()
        if "error" in data:
            return self.result(Status.DEGRADED, f"Bravia error {data['error']}", latency_ms=resp.latency_ms)
        power = data.get("result", [{}])[0].get("status", "unknown")
        return self.result(Status.UP, power, latency_ms=resp.latency_ms, metrics={"power": power})

    def _samsung(self):
        port = int(self.opt("port", 8001))
        resp = http_request(f"http://{self.device.host}:{port}/api/v2/", self.device.timeout_s)
        if resp.status != 200:
            return self.result(Status.DOWN, f"Samsung HTTP {resp.status}", latency_ms=resp.latency_ms)
        dev = resp.json().get("device", {})
        power = dev.get("PowerState", "on")
        metrics = {"power": power, **({"model": dev["modelName"]} if "modelName" in dev else {})}
        return self.result(Status.UP, power, latency_ms=resp.latency_ms, metrics=metrics)
