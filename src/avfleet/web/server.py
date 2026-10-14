"""Local dashboard: a static page plus a JSON status API, served with the standard library."""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from .. import maintenance, reboot
from ..config import Config
from ..store import Store

log = logging.getLogger("avfleet.web")

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
LOGO_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp"}


def logo_type(config: Config) -> str | None:
    """Content type of the configured logo, or None if there is no usable logo."""
    logo = config.web_logo
    if logo is None or not logo.is_file():
        return None
    return LOGO_TYPES.get(logo.suffix.lower())


def build_status(config: Config, store: Store, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    devices = []
    maint = store.active_maintenance(now)
    for dev in config.enabled_devices:
        latest = store.latest(dev.id)
        devices.append({
            "id": dev.id,
            "name": dev.name,
            "group": dev.group,
            "type": dev.type,
            "status": latest["status"] if latest else "unknown",
            "detail": latest["detail"] if latest else "waiting for first check",
            "latency_ms": latest["latency_ms"] if latest else None,
            "latency_typical_ms": store.latency_typical(dev.id, now=now),
            "metrics": latest["metrics"] if latest else {},
            "checked_at": latest["ts"] if latest else None,
            "since": store.last_change(dev.id),
            "uptime_24h": store.uptime(dev.id, now=now),
            "latency_24h": store.latency_series(dev.id, now=now),
            "self_healing": dev.remediation.action if dev.remediation else None,
            "maintenance": maint.get(dev.id),
            "reboot": {"label": dev.reboot.label} if dev.reboot else None,
        })
    return {"generated_at": now, "interval_s": config.interval_s, "devices": devices,
            "events": store.events(limit=100),
            "branding": {"title": config.web_title, "logo": logo_type(config) is not None},
            "controls": {"maintenance": True, "pin_required": bool(config.web_pin)},
            "mcp": ({**summary, "actions": config.mcp_actions} if (summary := store.mcp_summary(now)) else None)}


def make_handler(config: Config, store: Store) -> type[BaseHTTPRequestHandler]:
    static = resources.files("avfleet.web") / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = "avfleet"

        def log_message(self, fmt, *args):  # route to logging instead of stderr
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                body = json.dumps(build_status(config, store)).encode()
                return self._send(200, body, "application/json")
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if path == "/logo":
                ctype = logo_type(config)
                if ctype is None:
                    return self._send(404, b"not found", "text/plain")
                # The logo is the operator's own file; the CSP keeps an SVG from running script if opened directly.
                return self._send(200, config.web_logo.read_bytes(), ctype,
                                  {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            if "/" in name or name.startswith("."):
                return self._send(404, b"not found", "text/plain")
            f = static / name
            suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
            if not f.is_file() or suffix not in STATIC_TYPES:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, f.read_bytes(), STATIC_TYPES[suffix])

        # --- controls --------------------------------------------------------------------
        # The only write the dashboard can make is opening or closing a maintenance window.
        # Guard rails: JSON only (a plain HTML form on another site can't send it), a browser
        # Origin must match this host (stops other web pages driving it), small bodies only,
        # and an optional PIN for networks shared with guests or IoT devices.

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def _authorized(self) -> str | None:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return "JSON required"
            origin = self.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != self.headers.get("Host", ""):
                return "cross-origin request refused"
            if config.web_pin and not hmac.compare_digest(self.headers.get("X-AVFleet-PIN", ""), config.web_pin):
                return "pin"
            return None

        def do_POST(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/api/maintenance", "/api/maintenance/end", "/api/reboot"):
                return self._json(404, {"error": "not found"})
            problem = self._authorized()
            if problem == "pin":
                return self._json(401, {"error": "PIN required"})
            if problem:
                return self._json(403, {"error": problem})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 4096:
                return self._json(400, {"error": "bad request"})
            try:
                body = json.loads(self.rfile.read(length))
                device = str(body["device"])
                if path == "/api/reboot":
                    try:
                        outcome = reboot.run(store, config, device, via="dashboard")
                    except reboot.RebootError as exc:
                        return self._json(409 if "moments ago" in str(exc) else 400, {"error": str(exc)})
                    return self._json(200 if outcome.ok else 502, {"ok": outcome.ok, "message": outcome.message})
                if path.endswith("/end"):
                    ended = maintenance.end(store, config, [device], via="dashboard")
                    return self._json(200, {"ended": ended})
                minutes = int(body.get("minutes", 30))
                if not 1 <= minutes <= 7 * 24 * 60:
                    raise ValueError("minutes must be between 1 and 10080")
                reason = str(body.get("reason") or "")[:120]
                ids = maintenance.start(store, config, [device], minutes * 60, reason, via="dashboard")
                return self._json(200, {"started": ids, "minutes": minutes})
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return self._json(400, {"error": str(exc) or "bad request"})

    return Handler


def start_server(config: Config, store: Store) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((config.web_host, config.web_port), make_handler(config, store))
    threading.Thread(target=httpd.serve_forever, name="avfleet-web", daemon=True).start()
    log.info("dashboard on http://%s:%s", config.web_host, config.web_port)
    return httpd
