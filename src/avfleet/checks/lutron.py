"""Lutron processors (RadioRA 3, HomeWorks QSX; Ketra fixtures run through these).

Current Lutron processors expose LEAP, a JSON protocol over mutual-TLS on port 8081.
Full LEAP access needs a client certificate obtained by pairing with the processor.
Without one, a completed TLS handshake still proves the processor's integration service
is up. With a certificate (options cert/key/ca), the check also reads the processor's
/server info, and Ketra/zone status can be added in the same session later.
"""

from __future__ import annotations

import json
import socket
import ssl
import time

from ..models import Status
from .base import Check, register


@register("lutron_leap")
class LutronLeapCheck(Check):
    """Options: port (default 8081), cert, key, ca (paths, optional)."""

    def probe(self):
        port = int(self.opt("port", 8081))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        cert, key, ca = self.opt("cert"), self.opt("key"), self.opt("ca")
        if ca:
            ctx.load_verify_locations(ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
        if cert and key:
            ctx.load_cert_chain(cert, key)
        started = time.perf_counter()
        with socket.create_connection((self.device.host, port), timeout=self.device.timeout_s) as raw:
            try:
                with ctx.wrap_socket(raw) as tls:
                    latency = (time.perf_counter() - started) * 1000
                    if cert and key:
                        return self._read_server(tls, latency)
            except ssl.SSLError as exc:
                # A processor demanding a client cert we don't have still proves LEAP is listening.
                if "CERTIFICATE_REQUIRED" in str(exc) or "certificate required" in str(exc).lower():
                    return self.result(Status.UP, "LEAP listening (client cert required for detail)",
                                       started=started)
                raise
        return self.result(Status.UP, "LEAP TLS handshake OK", latency_ms=latency)

    def _read_server(self, tls: ssl.SSLSocket, latency: float):
        req = {"CommuniqueType": "ReadRequest", "Header": {"Url": "/server"}}
        tls.sendall((json.dumps(req) + "\r\n").encode())
        tls.settimeout(self.device.timeout_s)
        line = tls.recv(65536).split(b"\r\n", 1)[0]
        data = json.loads(line or b"{}")
        status = data.get("Header", {}).get("StatusCode", "")
        if status.startswith("200"):
            return self.result(Status.UP, "LEAP /server OK", latency_ms=latency)
        return self.result(Status.DEGRADED, f"LEAP status {status or 'none'}", latency_ms=latency)
