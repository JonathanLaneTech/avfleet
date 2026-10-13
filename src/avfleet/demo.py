"""``avfleet demo``: run the real monitor and dashboard against simulated devices.

Each simulator speaks the same protocol as the real device (YXC JSON, AirPlay plist,
Kaleidescape TCP, Bravia REST, UniFi controller API, a Roon port), all on localhost.
It backfills 24 hours of history so uptime and response-time charts have shape, and the
Roon simulator can be "crashed" to watch detection, alerting and self-healing live.
"""

from __future__ import annotations

import json
import logging
import plistlib
import random
import socket
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .actions.base import Action, register_action
from .config import Config, DeviceConfig, RemediationConfig
from .engine import Monitor
from .models import ActionResult, CheckResult, Event, Status
from .notifiers import LogNotifier
from .store import Store
from .web import start_server

log = logging.getLogger("avfleet.demo")

SWITCH_MAC, AP_MAC, GW_MAC = "02:00:00:00:00:02", "02:00:00:00:00:03", "02:00:00:00:00:01"


def _unifi_devices() -> list[dict]:
    ports = [{"port_idx": i, "up": i <= 11, "poe_power": f"{random.uniform(3, 14):.2f}" if i <= 8 else "0"}
             for i in range(1, 17)]
    return [
        {"mac": GW_MAC, "model": "UDMPROMAX", "state": 1, "num_sta": 31, "uptime": 2_100_000,
         "system-stats": {"cpu": f"{random.uniform(8, 20):.1f}", "mem": "47.0"}},
        {"mac": SWITCH_MAC, "model": "USPM16P", "state": 1, "num_sta": 14, "total_max_power": 180,
         "system-stats": {"cpu": f"{random.uniform(5, 12):.1f}", "mem": "38.0"}, "port_table": ports},
        {"mac": AP_MAC, "model": "U7PROMAX", "state": 1, "num_sta": random.randint(12, 19), "uptime": 900_000,
         "system-stats": {"cpu": f"{random.uniform(3, 9):.1f}", "mem": "29.0"}},
    ]


class _Http(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        time.sleep(random.uniform(0.004, 0.03))  # a little realistic latency
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/YamahaExtendedControl/v1/main/getStatus"):
            body = {"response_code": 0, "power": "on", "input": "net_radio", "volume": 52, "mute": False}
            return self._reply(200, json.dumps(body).encode())
        if self.path == "/info":
            info = {"model": self.server.airplay_model, "name": self.server.airplay_name, "osBuildVersion": "22K557"}
            return self._reply(200, plistlib.dumps(info, fmt=plistlib.FMT_BINARY), "application/x-apple-binary-plist")
        if self.path.startswith("/proxy/network/api/s/default/stat/device/"):
            mac = self.path.rsplit("/", 1)[-1]
            data = [d for d in _unifi_devices() if d["mac"] == mac]
            return self._reply(200, json.dumps({"meta": {"rc": "ok"}, "data": data}).encode())
        return self._reply(404, b"{}")

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/auth/login":
            return self._reply(200, b"{}", extra={"Set-Cookie": "TOKEN=demo; Path=/", "X-CSRF-Token": "demo"})
        if self.path == "/sony/system":
            return self._reply(200, json.dumps({"result": [{"status": "active"}], "id": 50}).encode())
        return self._reply(404, b"{}")


def _http_sim(model: str = "", name: str = "") -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Http)
    srv.airplay_model, srv.airplay_name = model, name
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


class _Kaleidescape(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            while data := self.request.recv(1024):
                if b"GET_DEVICE_POWER_STATE" in data:
                    seq = data.split(b"/")[1]
                    self.request.sendall(b"01/" + seq + b"/000:DEVICE_POWER_STATE:1:1:/89\r")
        except OSError:
            pass


class RoonSim:
    """A port that can be 'crashed' and 'restarted', standing in for the Roon Core."""

    def __init__(self):
        self.sock: socket.socket | None = None
        self.port = 0
        self.start()

    def start(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(16)
        threading.Thread(target=self._accept, args=(self.sock,), daemon=True).start()

    def _accept(self, s):
        while True:
            try:
                conn, _ = s.accept()
                conn.close()
            except OSError:
                return

    def crash(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept() so the port really closes
            except OSError:
                pass
            self.sock.close()
            self.sock = None
            time.sleep(0.1)


ROON = {"sim": None}


@register_action("demo_roon_restart")
class DemoRoonRestart(Action):
    def execute(self):
        if ROON["sim"] and ROON["sim"].sock is None:
            ROON["sim"].start()
        return ActionResult(self.name, True, "restarted Roon (simulated)")


def _backfill(store: Store, devices: list[DeviceConfig], interval: int, now: float) -> None:
    base = {d.id: random.uniform(4, 40) for d in devices}
    for dev in devices:
        t = now - 86400
        outage = dev.id == "kaleidescape"
        while t < now - interval:
            status = Status.UP
            if outage and now - 50000 < t < now - 49000:
                status = Status.DOWN
            if dev.id == "switch" and now - 30000 < t < now - 28800:
                status = Status.DEGRADED
            lat = None if status is Status.DOWN else round(base[dev.id] * random.uniform(0.7, 1.5), 1)
            store.add_result(CheckResult(dev.id, status, latency_ms=lat, detail="history", timestamp=t))
            t += interval * 5
    store.add_event(Event("kaleidescape", "alert", "Kaleidescape is DOWN: no reply to GET_DEVICE_POWER_STATE",
                          timestamp=now - 49800))
    store.add_event(Event("kaleidescape", "alert", "Kaleidescape recovered: on", timestamp=now - 49000))
    store.add_event(Event("switch", "state", "Switch: up -> degraded (expected port(s) down: 4)",
                          timestamp=now - 30000))
    store.add_event(Event("switch", "state", "Switch: degraded -> up", timestamp=now - 28800))


def build_demo(port: int = 8080, interval: int = 10) -> tuple[Config, Store, Monitor]:
    random.seed(7)
    web_sim = _http_sim()
    atv, homepod = _http_sim("AppleTV14,1", "Den"), _http_sim("AudioAccessory6,1", "Kitchen")
    ks = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Kaleidescape)
    ks.daemon_threads = True
    threading.Thread(target=ks.serve_forever, daemon=True).start()
    ROON["sim"] = RoonSim()
    local = f"127.0.0.1:{web_sim}"
    unifi = {"username": "demo", "password": "demo", "controller": f"http://{local}"}

    def d(id_, name, type_, group, host="127.0.0.1", **opts):
        return DeviceConfig(id=id_, name=name, type=type_, host=host, group=group, timeout_s=2, options=opts)

    devices = [
        d("udm", "Gateway (UDM Pro Max)", "unifi_device", "Network", mac=GW_MAC, **unifi),
        d("switch", "Switch (USW Pro Max 16 PoE)", "unifi_device", "Network", mac=SWITCH_MAC, **unifi),
        d("ap", "Access point (U7 Pro Max)", "unifi_device", "Network", mac=AP_MAC, **unifi),
        d("roon", "Roon Core", "roon", "Audio", port=ROON["sim"].port),
        d("yamaha", "Yamaha R-N2000A", "yamaha_yxc", "Audio", host=local),
        d("homepod", "HomePod", "homepod", "Audio", port=homepod),
        d("kaleidescape", "Kaleidescape", "kaleidescape", "Video", port=ks.server_address[1]),
        d("apple-tv", "Apple TV", "apple_tv", "Video", port=atv),
        d("tv", "Sony Bravia (K-75XR90)", "tv", "Video", host=local, protocol="sony_bravia"),
    ]
    devices[3].remediation = RemediationConfig(action="demo_roon_restart", after_failures=3, cooldown_s=60)
    db = Path(tempfile.mkdtemp(prefix="avfleet-demo-")) / "demo.db"
    config = Config(interval_s=interval, db_path=db, web_host="127.0.0.1", web_port=port, devices=devices)
    store = Store(db)
    _backfill(store, devices, interval, time.time())
    monitor = Monitor(config, store, [LogNotifier()])
    return config, store, monitor


def run_demo(port: int = 8080, crash_after: int = 30) -> None:
    config, store, monitor = build_demo(port)
    start_server(config, store)
    print(f"\n  avfleet demo running: open http://127.0.0.1:{port}\n"
          f"  The simulated Roon Core will crash in {crash_after}s; watch it get detected and restarted.\n"
          f"  Ctrl-C to stop.\n")
    def crash():
        log.warning("demo: crashing simulated Roon Core")
        ROON["sim"].crash()

    timer = threading.Timer(crash_after, crash)
    timer.daemon = True
    timer.start()
    try:
        monitor.run_forever()
    except KeyboardInterrupt:
        pass
