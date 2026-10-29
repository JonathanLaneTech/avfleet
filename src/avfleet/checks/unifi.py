"""UniFi devices (gateway, switches, access points) via the UniFi Network controller API.

On a UniFi OS console (UDM Pro / Pro Max, Cloud Gateway, Cloud Key Gen2+), the monitor logs
in with a *local, read-only* account at /api/auth/login and reads
/proxy/network/api/s/<site>/stat/device, which reports per-device state, uptime, clients,
CPU/memory, and for switches the full port table with link and PoE draw.

One session per controller is shared by all UniFi checks and re-established on 401.
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import threading
import time
import urllib.request
from typing import Any

from ..models import Status
from .base import Check, http_request, register

STATE_NAMES = {
    0: "disconnected", 1: "connected", 2: "pending adoption", 4: "upgrading",
    5: "provisioning", 6: "heartbeat missed", 7: "adopting", 9: "adoption failed", 11: "isolated",
}
NOTICES = {4: "Firmware updating", 5: "Provisioning", 7: "Adopting"}


class UnifiSession:
    _sessions: dict[str, UnifiSession] = {}
    _lock = threading.Lock()

    def __init__(self, base: str, username: str, password: str, verify_tls: bool, timeout: float):
        self.base, self.username, self.password = base.rstrip("/"), username, password
        self.timeout = timeout
        self.csrf: str | None = None
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self.logged_in = False
        self.lock = threading.Lock()

    @classmethod
    def get(cls, base: str, username: str, password: str, verify_tls: bool, timeout: float) -> UnifiSession:
        key = f"{base}|{username}"
        with cls._lock:
            if key not in cls._sessions:
                cls._sessions[key] = cls(base, username, password, verify_tls, timeout)
            return cls._sessions[key]

    @classmethod
    def reset_all(cls) -> None:
        with cls._lock:
            cls._sessions.clear()

    def login(self) -> None:
        body = json.dumps({"username": self.username, "password": self.password}).encode()
        resp = http_request(f"{self.base}/api/auth/login", self.timeout, method="POST", data=body,
                            headers={"Content-Type": "application/json"}, opener=self.opener)
        if resp.status != 200:
            raise PermissionError(f"UniFi login failed (HTTP {resp.status})")
        self.csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get("x-csrf-token")
        self.logged_in = True

    def get_json(self, path: str) -> tuple[Any, float]:
        with self.lock:
            for attempt in (1, 2):
                if not self.logged_in:
                    self.login()
                headers = {"X-CSRF-Token": self.csrf} if self.csrf else {}
                resp = http_request(f"{self.base}{path}", self.timeout, headers=headers, opener=self.opener)
                if resp.status == 401 and attempt == 1:
                    self.logged_in = False
                    continue
                if resp.status != 200:
                    raise ConnectionError(f"UniFi API HTTP {resp.status} for {path}")
                return resp.json(), resp.latency_ms
        raise ConnectionError("UniFi API: unauthorized after re-login")


def summarize_device(dev: dict[str, Any], expected_up_ports: list[int] | None = None) -> tuple[Status, str, dict]:
    """Turn one stat/device record into (status, detail, metrics). Pure function, easy to test."""
    state = int(dev.get("state", 0))
    metrics: dict[str, Any] = {
        "model": dev.get("model"),
        "version": dev.get("version"),
        "uptime_s": dev.get("uptime"),
        "clients": dev.get("num_sta"),
    }
    stats = dev.get("system-stats") or {}
    for k in ("cpu", "mem"):
        if k in stats:
            try:
                metrics[f"{k}_pct"] = float(stats[k])
            except (TypeError, ValueError):
                pass
    if dev.get("upgradable"):
        metrics["upgrade_available"] = True

    ports = dev.get("port_table") or []
    down_expected: list[int] = []
    if ports:
        up = [p for p in ports if p.get("up")]
        metrics["ports_up"] = len(up)
        metrics["ports_total"] = len(ports)
        metrics["poe_watts"] = round(sum(float(p.get("poe_power") or 0) for p in ports), 1)
        if dev.get("total_max_power"):
            metrics["poe_budget_watts"] = dev["total_max_power"]
        by_idx = {p.get("port_idx"): p for p in ports}
        down_expected = [i for i in (expected_up_ports or []) if not by_idx.get(i, {}).get("up")]
        if down_expected:
            metrics["expected_ports_down"] = down_expected

    if state in NOTICES:
        metrics["notice"] = NOTICES[state]
    if state != 1:
        name = STATE_NAMES.get(state, f"state {state}")
        status = Status.DOWN if state in (0, 6, 9) else Status.DEGRADED
        return status, name, metrics
    parts = []
    if metrics.get("clients") is not None:
        parts.append(f"{metrics['clients']} Clients")
    if ports:
        parts.append(f"{metrics['ports_up']}/{metrics['ports_total']} Ports Up")
        if metrics["poe_watts"]:
            parts.append(f"PoE {metrics['poe_watts']} W")
    if down_expected:
        return Status.DEGRADED, f"expected port(s) down: {', '.join(map(str, down_expected))}", metrics
    return Status.UP, " · ".join(parts) or "connected", metrics


@register("unifi_device")
class UnifiDeviceCheck(Check):
    """Options: mac (device MAC), username, password (use ${ENV}), site (default "default"),
    controller (default https://<host>), verify_tls (default false: consoles use self-signed certs),
    expected_up_ports ([int]: ports that must have link, e.g. the uplink and the Roon Core).
    """

    def probe(self):
        mac = str(self.opt("mac", "")).lower()
        user, pw = self.opt("username"), self.opt("password")
        if not mac or not user or not pw:
            return self.result(Status.UNKNOWN, "needs mac, username and password options")
        session = UnifiSession.get(self.opt("controller", f"https://{self.device.host}"), user, pw,
                                   bool(self.opt("verify_tls", False)), self.device.timeout_s)
        started = time.perf_counter()
        site = self.opt("site", "default")
        payload, latency = session.get_json(f"/proxy/network/api/s/{site}/stat/device/{mac}")
        devices = payload.get("data", []) if isinstance(payload, dict) else []
        dev = next((d for d in devices if str(d.get("mac", "")).lower() == mac), None)
        if dev is None:
            return self.result(Status.DOWN, f"{mac} not found on controller", started=started)
        status, detail, metrics = summarize_device(dev, self.opt("expected_up_ports"))
        return self.result(status, detail, latency_ms=latency, metrics=metrics)
