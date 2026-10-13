"""Check plug-in base class, registry and shared network helpers.

A check answers one question for one device: is it doing its job right now?
Each device type registers a subclass with ``@register("type-name")``; the config's
``type`` field selects it. Checks must never raise: failures become a CheckResult.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from ..config import DeviceConfig
from ..models import CheckResult, Status

_REGISTRY: dict[str, type[Check]] = {}


def register(type_name: str) -> Callable[[type[Check]], type[Check]]:
    def deco(cls: type[Check]) -> type[Check]:
        if type_name in _REGISTRY:
            raise ValueError(f"check type '{type_name}' registered twice")
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return deco


def check_types() -> list[str]:
    return sorted(_REGISTRY)


def build_check(device: DeviceConfig) -> Check:
    try:
        cls = _REGISTRY[device.type]
    except KeyError:
        raise ValueError(
            f"device '{device.id}': unknown type '{device.type}' (known: {', '.join(check_types())})"
        ) from None
    return cls(device)


class Check:
    type_name: ClassVar[str] = ""

    def __init__(self, device: DeviceConfig):
        self.device = device

    def run(self) -> CheckResult:
        """Run the check, converting any unexpected exception into a DOWN result."""
        start = time.perf_counter()
        try:
            return self.probe()
        except Exception as exc:  # noqa: BLE001 - a check must never crash the monitor
            return self.result(Status.DOWN, detail=f"{type(exc).__name__}: {exc}", started=start)

    def probe(self) -> CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def opt(self, key: str, default: Any = None) -> Any:
        return self.device.options.get(key, default)

    def result(
        self,
        status: Status,
        detail: str = "",
        latency_ms: float | None = None,
        metrics: dict[str, Any] | None = None,
        started: float | None = None,
    ) -> CheckResult:
        if latency_ms is None and started is not None:
            latency_ms = (time.perf_counter() - started) * 1000
        return CheckResult(
            device_id=self.device.id,
            status=status,
            latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
            detail=detail,
            metrics=metrics or {},
        )


# --- helpers ---------------------------------------------------------------------------


def tcp_connect(host: str, port: int, timeout: float) -> float:
    """Open and close a TCP connection. Returns connect time in ms; raises OSError on failure."""
    start = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return (time.perf_counter() - start) * 1000


@dataclass(slots=True)
class HttpResponse:
    status: int
    body: bytes
    latency_ms: float
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def http_request(
    url: str,
    timeout: float,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    verify_tls: bool = True,
    opener: urllib.request.OpenerDirector | None = None,
) -> HttpResponse:
    """HTTP(S) request returning status, body and latency. HTTP error codes do not raise."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    ctx = None
    if url.startswith("https://") and not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    start = time.perf_counter()
    try:
        if opener is not None:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        with resp:
            body = resp.read()
            status = resp.status
            hdrs = dict(resp.headers.items())
    except urllib.error.HTTPError as err:
        body = err.read() or b""
        status = err.code
        hdrs = dict(err.headers.items()) if err.headers else {}
    return HttpResponse(status, body, (time.perf_counter() - start) * 1000, hdrs)
