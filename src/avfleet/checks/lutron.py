"""Lutron processors (HomeWorks QSX, RadioRA 3) and the Ketra fixtures behind them.

Lutron's current processors speak LEAP: JSON messages, one per line, over mutual TLS on
port 8081. Two modes:

* **Unpaired** (no client certificate): a completed TLS handshake, or a processor that
  demands a certificate, proves the integration service is up. Used for gateways too.
* **Paired** (certificate from ``lap-pair``, the pairing tool in the pylutron-caseta
  project): the check walks the processor's own view of the system and reports per-fixture
  health. Certificates are found automatically in ``~/.config/pylutron_caseta/<host>.*``.

The walk (QSX does not support bulk status queries, so it goes device by device):

    /project                         -> master device list (processor, gateways)
    /device/<id>                     -> model, firmware, the links it owns
    /link/<id>/associatedlinknode    -> every device on that link (e.g. Ketra on Clear Connect X)
    /device/<id>/status              -> Availability, ConfigurationTransferStatus
    /zone/<id> + /zone/<id>/status   -> zone name, level, colour temperature

What LEAP on a QSX does *not* tell you: whether a fixture is physically present. Field
testing (unpowering a Ketra A20) showed ``Availability`` staying ``Unknown``, the zone still
reporting its commanded level and colour, and the fixture remaining in the link's node list.
So zone state is shown as "set to", and missing fixtures can only be detected if a future
firmware starts populating ``Availability`` (the check already handles ``Unavailable``).

``/link/<id>`` itself is never read: on Clear Connect X links it includes the network
master key, which this tool has no reason to handle. Bodies are parsed, never logged.
"""

from __future__ import annotations

import itertools
import json
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Status
from .base import Check, register

PAIRING_DIR = Path("~/.config/pylutron_caseta").expanduser()
TRANSFER_PROBLEMS = {"Required", "InProgress", "Pending", "Failed"}


class LeapError(RuntimeError):
    pass


class LeapClient:
    """Minimal LEAP request/response client over an already-connected socket."""

    _tags = itertools.count(1)

    def __init__(self, sock: socket.socket, timeout: float):
        self.sock = sock
        self.sock.settimeout(timeout)
        self.timeout = timeout
        self._buf = b""

    @classmethod
    def connect_tls(cls, host: str, port: int, timeout: float, cert: str, key: str, ca: str | None) -> LeapClient:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False  # processors present a certificate for their own name, not the IP
        if ca:
            ctx.load_verify_locations(ca)
            ctx.verify_mode = ssl.CERT_REQUIRED
        else:
            ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(cert, key)
        raw = socket.create_connection((host, port), timeout=timeout)
        try:
            return cls(ctx.wrap_socket(raw), timeout)
        except Exception:
            raw.close()
            raise

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def read(self, url: str) -> tuple[str, dict[str, Any]]:
        """Send a ReadRequest; return (status code line, body). Unrelated messages are skipped."""
        tag = f"avfleet-{next(self._tags)}"
        msg = {"CommuniqueType": "ReadRequest", "Header": {"Url": url, "ClientTag": tag}}
        self.sock.sendall((json.dumps(msg) + "\r\n").encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            while b"\r\n" in self._buf:
                line, self._buf = self._buf.split(b"\r\n", 1)
                if not line.strip():
                    continue
                reply = json.loads(line)
                header = reply.get("Header", {})
                if header.get("ClientTag") == tag:
                    return header.get("StatusCode", ""), reply.get("Body") or {}
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                raise LeapError("processor closed the connection")
            self._buf += chunk
        raise LeapError(f"no reply to {url}")

    def body(self, url: str) -> dict[str, Any]:
        status, body = self.read(url)
        if not status.startswith("200"):
            raise LeapError(f"{url}: {status or 'no status'}")
        return body


@dataclass
class LutronDevice:
    href: str
    name: str
    device_type: str
    model: str = ""
    availability: str = "Unknown"
    transfer: str = ""
    firmware: str = ""
    firmware_package: str = ""
    zones: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_ketra(self) -> bool:
        return self.device_type.lower().startswith("ketra")

    @property
    def label(self) -> str:
        """Prefer the zone name ("Lamp") over the device name, which is often just "1"."""
        names = [z["name"] for z in self.zones if z.get("name")]
        return names[0] if names else self.name


def _href_id(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


def walk(client: LeapClient) -> dict[str, Any]:
    """Collect the processor's view of every device and its zones."""
    project = client.body("/project").get("Project", {})
    master = [d["href"] for d in project.get("MasterDeviceList", {}).get("Devices", [])]
    seen: dict[str, dict[str, Any]] = {}
    queue = list(master)
    link_hrefs: list[str] = []
    while queue:
        href = queue.pop(0)
        if href in seen:
            continue
        dev = client.body(href).get("Device", {})
        seen[href] = dev
        for link in dev.get("OwnedLinks") or []:
            if link["href"] not in link_hrefs:
                link_hrefs.append(link["href"])
                status, nodes = client.read(f"{link['href']}/associatedlinknode")
                if status.startswith("200"):
                    for node in nodes.get("LinkNodes", []):
                        parent = node.get("Parent", {}).get("href")
                        if parent and parent not in seen:
                            queue.append(parent)

    processor: LutronDevice | None = None
    devices: list[LutronDevice] = []
    for href, dev in seen.items():
        item = LutronDevice(
            href=href,
            name=dev.get("Name", _href_id(href)),
            device_type=dev.get("DeviceType", ""),
            model=dev.get("ModelNumber", ""),
            firmware=(dev.get("FirmwareImage") or {}).get("Firmware", {}).get("DisplayName", ""),
            firmware_package=(dev.get("DeviceFirmwarePackage") or {}).get("Package", {}).get("DisplayName", ""),
        )
        status, st = client.read(f"{href}/status")
        if status.startswith("200"):
            ds = st.get("DeviceStatus", {})
            item.availability = ds.get("Availability", "Unknown") or "Unknown"
            item.transfer = ds.get("ConfigurationTransferStatus", "") or ""
        for z in dev.get("LocalZones") or []:
            zone = {"href": z["href"], "name": ""}
            s1, zbody = client.read(z["href"])
            if s1.startswith("200"):
                zone["name"] = zbody.get("Zone", {}).get("Name", "")
            s2, zst = client.read(f"{z['href']}/status")
            if s2.startswith("200"):
                zs = zst.get("ZoneStatus", {})
                zone["level"] = zs.get("Level")
                zone["kelvin"] = (zs.get("ColorTuningStatus") or {}).get("WhiteTuningLevel", {}).get("Kelvin")
            item.zones.append(zone)
        if dev.get("IsThisDevice"):
            processor = item
        else:
            devices.append(item)
    return {"project": project.get("Name", ""), "processor": processor, "devices": devices}


def summarize(topology: dict[str, Any]) -> tuple[Status, str, dict[str, Any]]:
    """Turn a walked topology into (status, detail, metrics). Pure, so it is easy to test."""
    processor: LutronDevice | None = topology.get("processor")
    devices: list[LutronDevice] = topology.get("devices", [])
    ketra = [d for d in devices if d.is_ketra]
    unavailable = [d for d in devices if d.availability == "Unavailable"]
    transfer = [d for d in [processor, *devices] if d and d.transfer in TRANSFER_PROBLEMS]
    zones = [z for d in ketra for z in d.zones]
    metrics: dict[str, Any] = {
        "ketra_total": len(ketra),
        "ketra_unavailable": len([d for d in ketra if d.availability == "Unavailable"]),
        "devices_total": len(devices),
        "zones_on": len([z for z in zones if (z.get("level") or 0) > 0]),
    }
    if processor:
        if processor.firmware:
            metrics["firmware"] = processor.firmware
        if processor.firmware_package:
            metrics["firmware_package"] = processor.firmware_package
        if processor.model:
            metrics["model"] = processor.model
    if unavailable:
        metrics["unavailable"] = [d.label for d in unavailable]
        names = ", ".join(d.label for d in unavailable[:3]) + ("…" if len(unavailable) > 3 else "")
        return Status.DEGRADED, f"{len(unavailable)} unavailable: {names}", metrics
    if transfer:
        return Status.DEGRADED, f"programming transfer {transfer[0].transfer.lower()} on {transfer[0].label}", metrics
    noun = "Ketra fixture" if len(ketra) == 1 else "Ketra fixtures"
    detail = f"{len(ketra)} {noun}"
    if len(zones) == 1:
        z = zones[0]
        level = z.get("level")
        # The processor reports the level it last commanded, not measured output (see module docstring).
        state = "set off" if not level else f"set to {level}%" + (f" · {z['kelvin']} K" if z.get("kelvin") else "")
        detail += f" · {z.get('name') or 'zone'} {state}"
    elif zones:
        detail += f" · {metrics['zones_on']}/{len(zones)} zones on"
    return Status.UP, detail, metrics


def find_pairing(host: str, directory: Path = PAIRING_DIR) -> tuple[str, str, str | None] | None:
    """Locate lap-pair's certificate files for this host, if pairing has been done."""
    cert, key, ca = directory / f"{host}.crt", directory / f"{host}.key", directory / f"{host}-bridge.crt"
    if cert.is_file() and key.is_file():
        return str(cert), str(key), str(ca) if ca.is_file() else None
    return None


@register("lutron_leap")
class LutronLeapCheck(Check):
    """Options: port (default 8081); cert, key, ca (default: lap-pair files for this host, if present);
    paired (default true: use certificates when available; false forces the handshake-only check)."""

    def probe(self):
        port = int(self.opt("port", 8081))
        creds = None
        if self.opt("paired", True):
            if self.opt("cert") and self.opt("key"):
                creds = (self.opt("cert"), self.opt("key"), self.opt("ca"))
            else:
                creds = find_pairing(self.device.host)
        if creds:
            return self._paired(port, *creds)
        return self._handshake(port)

    def _paired(self, port: int, cert: str, key: str, ca: str | None):
        started = time.perf_counter()
        client = LeapClient.connect_tls(self.device.host, port, self.device.timeout_s, cert, key, ca)
        try:
            client.body("/server")
            latency = (time.perf_counter() - started) * 1000
            topology = walk(client)
        finally:
            client.close()
        status, detail, metrics = summarize(topology)
        return self.result(status, detail, latency_ms=latency, metrics=metrics)

    def _handshake(self, port: int):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        started = time.perf_counter()
        with socket.create_connection((self.device.host, port), timeout=self.device.timeout_s) as raw:
            try:
                with ctx.wrap_socket(raw):
                    latency = (time.perf_counter() - started) * 1000
            except ssl.SSLError as exc:
                # A processor demanding a client cert we don't have still proves LEAP is listening.
                if "CERTIFICATE_REQUIRED" in str(exc) or "certificate required" in str(exc).lower():
                    return self.result(Status.UP, "LEAP listening (not paired)", started=started)
                raise
        return self.result(Status.UP, "LEAP TLS handshake OK", latency_ms=latency)
