"""Shared fixtures: in-process fake devices (HTTP and raw TCP) so tests never touch a real network."""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from avfleet.checks.unifi import UnifiSession
from avfleet.config import DeviceConfig


class FakeHttp:
    """Routes: {(method, path): handler(request) -> (status, body, headers)} or a static tuple."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = {"method": method, "path": self.path, "body": body,
                       "headers": {k.lower(): v for k, v in self.headers.items()}}
                fake.requests.append(req)
                route = fake.routes.get((method, self.path))
                if route is None:
                    status, payload, headers = 404, b"nope", {}
                elif callable(route):
                    status, payload, headers = route(req)
                else:
                    status, payload, headers = route
                if isinstance(payload, str):
                    payload = payload.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_http():
    srv = FakeHttp()
    yield srv
    srv.close()


class FakeTcp:
    """A line-oriented TCP server; ``responder(data) -> bytes`` answers each chunk received."""

    def __init__(self, responder: Callable[[bytes], bytes]):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(2)
                try:
                    while data := self.request.recv(4096):
                        reply = responder(data)
                        if reply:
                            self.request.sendall(reply)
                except (TimeoutError, OSError):
                    pass

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_tcp():
    servers = []

    def make(responder):
        s = FakeTcp(responder)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


@pytest.fixture
def closed_port() -> int:
    """A localhost port with nothing listening."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _reset_unifi_sessions():
    UnifiSession.reset_all()
    yield
    UnifiSession.reset_all()


def device(type_: str, host: str = "127.0.0.1", **options) -> DeviceConfig:
    return DeviceConfig(id=f"test-{type_}", name=f"Test {type_}", type=type_, host=host,
                        timeout_s=2.0, options=options)
