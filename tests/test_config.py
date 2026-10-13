import ipaddress
import re
from pathlib import Path

import pytest

from avfleet.checks import build_check
from avfleet.config import ConfigError, load_config, parse_config
from avfleet.notifiers import PushoverNotifier, build_notifiers

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.toml"
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def test_env_expansion_and_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AVF_USER", raising=False)
    monkeypatch.setenv("AVF_PRESET", "kept")
    env = tmp_path / "secrets.env"
    env.write_text('# comment\nexport AVF_USER="monitor"\nAVF_PRESET=overwritten\n')
    cfg = parse_config({
        "monitor": {"env_file": str(env)},
        "devices": [{"id": "a", "type": "tcp", "host": "h",
                     "options": {"user": "${AVF_USER}", "p": "${AVF_PRESET}", "missing": "x${AVF_NOPE}y"}}],
    })
    opts = cfg.devices[0].options
    assert opts == {"user": "monitor", "p": "kept", "missing": "xy"}


@pytest.mark.parametrize("devices,msg", [
    ([{"type": "tcp", "host": "h"}], "missing 'id'"),
    ([{"id": "a", "type": "tcp", "host": "h"}, {"id": "a", "type": "tcp", "host": "h"}], "duplicate"),
    ([{"id": "a", "type": "tcp", "host": "h", "remediation": {"after_failures": 2}}], "action"),
])
def test_config_errors(devices, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({"devices": devices})


def test_missing_file():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.toml")


def test_example_config_is_valid_and_builds_every_check(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(EXAMPLE)
    assert len(cfg.devices) >= 10
    for dev in cfg.devices:
        build_check(dev)
    roon = next(d for d in cfg.devices if d.id == "roon")
    assert roon.remediation and roon.remediation.action == "roon_restart"
    assert not next(d for d in cfg.devices if d.id == "lutron").enabled


def test_example_config_contains_no_real_network_details():
    """Guard rail for a public repo: only documentation addresses, made-up MACs, no inline secrets."""
    text = EXAMPLE.read_text()
    for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
        addr = ipaddress.ip_address(ip)
        allowed = addr.is_loopback or addr.is_unspecified or any(addr in n for n in DOC_NETS)
        assert allowed, f"non-documentation address {ip}"
    for mac in re.findall(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", text):
        assert mac.lower().startswith("02:00:00"), f"real-looking MAC {mac}"
    for key in ("password", "token", "user", "psk", "username"):
        for value in re.findall(rf'^\s*{key}\s*=\s*"([^"]*)"', text, re.M):
            assert value.startswith("${"), f"{key} must be an ${{ENV}} reference, found {value!r}"


def test_gitignore_excludes_real_config():
    ignored = (ROOT / ".gitignore").read_text().split()
    assert "config.toml" in ignored and "*.env" in ignored


def test_notifiers_skip_misconfigured_pushover():
    built = build_notifiers([{"type": "pushover", "token": "", "user": ""}])
    assert not any(isinstance(n, PushoverNotifier) for n in built)


def test_pushover_posts(fake_http):
    fake_http.routes[("POST", "/1/messages.json")] = (200, '{"status":1}', {})
    n = PushoverNotifier("tok", "usr", url=f"http://127.0.0.1:{fake_http.port}/1/messages.json")
    assert n.send("Roon is DOWN", "no response", priority=1)
    body = fake_http.requests[-1]["body"].decode()
    assert "token=tok" in body and "title=Roon+is+DOWN" in body and "priority=1" in body
