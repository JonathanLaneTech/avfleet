"""Protocol-level checks usable for any device: TCP port and HTTP endpoint."""

from __future__ import annotations

import time

from ..models import Status
from .base import Check, http_request, register, tcp_connect


@register("tcp")
class TcpCheck(Check):
    """UP if every listed port accepts a connection. Options: ports = [int, ...]."""

    def probe(self):
        ports = self.opt("ports") or [self.opt("port")]
        if not ports or ports == [None]:
            return self.result(Status.UNKNOWN, "no 'port' or 'ports' configured")
        times = []
        for port in ports:
            try:
                times.append(tcp_connect(self.device.host, int(port), self.device.timeout_s))
            except OSError as exc:
                return self.result(Status.DOWN, f"port {port}: {exc.strerror or exc}")
        return self.result(Status.UP, f"ports {', '.join(map(str, ports))} open", latency_ms=max(times))


@register("http")
class HttpCheck(Check):
    """UP if an HTTP(S) endpoint returns the expected status.

    Options: url (or path + scheme/port), expect_status (default 200), verify_tls (default true).
    """

    def probe(self):
        url = self.opt("url")
        if not url:
            scheme = self.opt("scheme", "http")
            port = self.opt("port")
            netloc = f"{self.device.host}:{port}" if port else self.device.host
            url = f"{scheme}://{netloc}{self.opt('path', '/')}"
        expect = int(self.opt("expect_status", 200))
        started = time.perf_counter()
        resp = http_request(url, self.device.timeout_s, verify_tls=bool(self.opt("verify_tls", True)))
        if resp.status == expect:
            return self.result(Status.UP, f"HTTP {resp.status}", latency_ms=resp.latency_ms)
        return self.result(Status.DOWN, f"HTTP {resp.status} (expected {expect})", started=started)
