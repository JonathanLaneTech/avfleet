"""Configuration loading.

The config file is TOML. Secrets never live in it: any string value may reference an
environment variable as ``${NAME}``, and an optional ``env_file`` (KEY=VALUE lines,
chmod 600) is loaded first so launchd jobs get the same secrets as an interactive shell.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class RemediationConfig:
    action: str
    after_failures: int = 3
    cooldown_s: int = 900
    max_per_hour: int = 3
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeviceConfig:
    id: str
    name: str
    type: str
    host: str
    group: str = "Other"
    enabled: bool = True
    timeout_s: float = 5.0
    options: dict[str, Any] = field(default_factory=dict)
    remediation: RemediationConfig | None = None


@dataclass(slots=True)
class Config:
    interval_s: int = 60
    db_path: Path = Path("~/.avfleet/avfleet.db").expanduser()
    history_days: int = 7
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    fail_threshold: int = 3
    notify_recovery: bool = True
    notifiers: list[dict[str, Any]] = field(default_factory=list)
    devices: list[DeviceConfig] = field(default_factory=list)

    @property
    def enabled_devices(self) -> list[DeviceConfig]:
        return [d for d in self.devices if d.enabled]


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables already set."""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def expand_env(value: Any) -> Any:
    """Recursively replace ${NAME} in strings. Unset variables become empty strings."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def _device(raw: dict[str, Any], index: int) -> DeviceConfig:
    for key in ("id", "type", "host"):
        if not raw.get(key):
            raise ConfigError(f"device #{index + 1} is missing '{key}'")
    rem = raw.get("remediation")
    remediation = None
    if rem:
        if "action" not in rem:
            raise ConfigError(f"device '{raw['id']}': remediation needs an 'action'")
        remediation = RemediationConfig(
            action=rem["action"],
            after_failures=int(rem.get("after_failures", 3)),
            cooldown_s=int(rem.get("cooldown_s", 900)),
            max_per_hour=int(rem.get("max_per_hour", 3)),
            options=dict(rem.get("options", {})),
        )
    return DeviceConfig(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        type=raw["type"],
        host=raw["host"],
        group=raw.get("group", "Other"),
        enabled=bool(raw.get("enabled", True)),
        timeout_s=float(raw.get("timeout_s", 5.0)),
        options=dict(raw.get("options", {})),
        remediation=remediation,
    )


def parse_config(data: dict[str, Any]) -> Config:
    monitor = data.get("monitor", {})
    env_file = monitor.get("env_file")
    if env_file:
        p = Path(env_file).expanduser()
        if p.exists():
            load_env_file(p)
    data = expand_env(data)
    monitor = data.get("monitor", {})
    web = data.get("web", {})
    alerts = data.get("alerts", {})

    devices = [_device(d, i) for i, d in enumerate(data.get("devices", []))]
    ids = [d.id for d in devices]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate device ids: {', '.join(sorted(dupes))}")

    return Config(
        interval_s=int(monitor.get("interval_s", 60)),
        db_path=Path(monitor.get("db_path", "~/.avfleet/avfleet.db")).expanduser(),
        history_days=int(monitor.get("history_days", 7)),
        web_host=web.get("host", "127.0.0.1"),
        web_port=int(web.get("port", 8080)),
        fail_threshold=int(alerts.get("fail_threshold", 3)),
        notify_recovery=bool(alerts.get("notify_recovery", True)),
        notifiers=list(data.get("notifiers", [])),
        devices=devices,
    )


def load_config(path: str | Path) -> Config:
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open("rb") as fh:
        return parse_config(tomllib.load(fh))
