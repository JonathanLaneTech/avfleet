"""Kaleidescape players and servers via the Kaleidescape control protocol (TCP 10000).

Messages are ``<device>/<sequence>/<COMMAND>:`` terminated by CR; replies look like
``01/1/000:DEVICE_POWER_STATE:1:1:/89`` where ``000`` is the status code (0 = success),
the fields follow the reply name, and an optional ``/NN`` checksum ends the line.
The player may interleave unsolicited events, so replies are matched by sequence number.

Field meanings should be confirmed against Kaleidescape's control protocol reference for
your firmware; the parser is deliberately tolerant.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

from ..models import Status
from .base import Check, register


@dataclass(slots=True)
class KReply:
    device: str
    seq: str
    status: int
    name: str
    fields: list[str]


def build_command(device: str, seq: int, command: str) -> bytes:
    return f"{device}/{seq}/{command}:\r".encode("ascii")


def parse_reply(line: str) -> KReply | None:
    line = line.strip()
    parts = line.split("/", 2)
    if len(parts) < 3:
        return None
    device, seq, rest = parts
    if ":/" in rest:  # strip trailing checksum
        rest = rest[: rest.rfind(":/") + 1]
    fields = rest.split(":")
    if len(fields) < 2 or not fields[0].isdigit():
        return None
    return KReply(device, seq, int(fields[0]), fields[1], [f for f in fields[2:] if f != ""])


@register("kaleidescape")
class KaleidescapeCheck(Check):
    """Options: port (default 10000), device_id (default "01")."""

    def probe(self):
        port = int(self.opt("port", 10000))
        dev = str(self.opt("device_id", "01"))
        seq = 1
        started = time.perf_counter()
        with socket.create_connection((self.device.host, port), timeout=self.device.timeout_s) as sock:
            sock.settimeout(self.device.timeout_s)
            sock.sendall(build_command(dev, seq, "GET_DEVICE_POWER_STATE"))
            reply = self._read_reply(sock, str(seq))
        latency = (time.perf_counter() - started) * 1000
        if reply is None:
            return self.result(Status.DOWN, "no reply to GET_DEVICE_POWER_STATE", latency_ms=latency)
        if reply.status != 0:
            return self.result(Status.DEGRADED, f"error status {reply.status:03d}", latency_ms=latency)
        power = reply.fields[0] if reply.fields else "?"
        state = {"1": "on", "0": "standby"}.get(power, f"power state {power}")
        return self.result(Status.UP, state, latency_ms=latency, metrics={"power": state})

    @staticmethod
    def _read_reply(sock: socket.socket, seq: str) -> KReply | None:
        buf = b""
        deadline = time.monotonic() + sock.gettimeout()
        while time.monotonic() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.replace(b"\n", b"\r").split(b"\r")
            for raw in lines:
                reply = parse_reply(raw.decode("ascii", "replace"))
                if reply and reply.seq == seq:
                    return reply
        return None
