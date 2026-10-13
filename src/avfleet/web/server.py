"""Local dashboard: a static page plus a JSON status API, served with the standard library."""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

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
            "metrics": latest["metrics"] if latest else {},
            "checked_at": latest["ts"] if latest else None,
            "since": store.last_change(dev.id),
            "uptime_24h": store.uptime(dev.id, now=now),
            "latency_24h": store.latency_series(dev.id, now=now),
            "self_healing": dev.remediation.action if dev.remediation else None,
        })
    return {"generated_at": now, "interval_s": config.interval_s, "devices": devices,
            "events": store.events(limit=100),
            "branding": {"title": config.web_title, "logo": logo_type(config) is not None}}


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

    return Handler


def start_server(config: Config, store: Store) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((config.web_host, config.web_port), make_handler(config, store))
    threading.Thread(target=httpd.serve_forever, name="avfleet-web", daemon=True).start()
    log.info("dashboard on http://%s:%s", config.web_host, config.web_port)
    return httpd
