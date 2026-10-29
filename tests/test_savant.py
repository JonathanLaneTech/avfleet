"""Savant host check: parsing System Monitor fields, the info schedule, and failure modes."""

import subprocess

import pytest

from avfleet.actions import build_action
from avfleet.checks import savant
from avfleet.checks.savant import SEP, SavantHostCheck, parse_info
from avfleet.config import DeviceConfig
from avfleet.models import Status

OSVER = """MANUFACTURER=Savant Systems
SAVANT_OS_VERSION=12.0.0
SAVANT_IMAGE_TYPE=host_audio
BUILD_ID=555
SAVANT_PRODUCT_VERSION_MAJOR=12
SAVANT_PRODUCT_VERSION_MINOR=0
SAVANT_PRODUCT_VERSION_MAINT=0
INSTALL_DATE=2026-10-25T02:47:24+00:00
CLOUD_ENV=cbeta1
"""
PS_OK = """RPM 1129 2.2 1.5 307780 32300 ? SNl 15:07 0:53 /usr/local/bin/startupManager
RPM 1168 5.7 1.5 364588 31420 ? SNsl 15:07 2:18 /usr/local/bin/rpmMonitord
RPM 1531 2.3 2.3 538844 48404 ? SNsl 15:07 0:55 /usr/local/bin/avc Healing House Host#RacePointMedia_host
RPM 1592 3.6 1.4 479660 29452 ? SNsl 15:07 1:27 /usr/local/bin/rpmStateCenter
"""


def output(ps=PS_OK):
    return f"\n{SEP}\n".join([OSVER, "4.14.78+g78f1513efacd", "001aae11cd070000", "PAV-SIPA1SM-00\x00", ps])


def dev():
    return DeviceConfig(id="savant", name="Savant Smart Host", type="savant_host", host="h",
                        options={"ssh_key": "~/.avfleet/k"})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    savant._CACHE.clear()
    monkeypatch.setattr(savant, "tcp_connect", lambda h, p, t: 3.0)


def fake_ssh(monkeypatch, stdout="", rc=0, stderr=""):
    calls = []

    def run(self, script, timeout=20):
        calls.append(script)
        return subprocess.CompletedProcess([], rc, stdout, stderr)

    monkeypatch.setattr(SavantHostCheck, "run_ssh", run)
    return calls


def test_parse_matches_system_monitor():
    info = parse_info(output())
    assert info["name"] == "Healing House Host"
    assert info["unique_id"] == "001AAE11CD070000"
    assert info["version"] == "12.0-cbeta1:555"
    assert info["installed"] == "2026-10-25"
    assert info["os_version"] == "4.14.78"
    assert info["model"] == "PAV-SIPA1SM-00"
    assert all(info["processes"].values())


def test_up_shows_version_os_model(monkeypatch):
    fake_ssh(monkeypatch, output())
    r = SavantHostCheck(dev()).run()
    assert r.status is Status.UP
    assert r.detail == "Software 12.0-cbeta1:555\nSystem Running"


def test_stopped_state_center_is_down(monkeypatch):
    ps = "\n".join(line for line in PS_OK.splitlines() if "rpmStateCenter" not in line)
    fake_ssh(monkeypatch, output(ps))
    r = SavantHostCheck(dev()).run()
    assert r.status is Status.DOWN
    assert r.detail == "Software 12.0-cbeta1:555\nSavant Services Stopped: rpmStateCenter"


def test_ssh_failure_is_degraded_not_down(monkeypatch):
    fake_ssh(monkeypatch, rc=255, stderr="RPM@h: Permission denied (publickey,password).")
    r = SavantHostCheck(dev()).run()
    assert r.status is Status.DEGRADED
    assert "Permission denied" in r.detail


def test_info_read_only_every_interval(monkeypatch):
    calls = fake_ssh(monkeypatch, output())
    clock = [1000.0]
    monkeypatch.setattr(savant.time, "time", lambda: clock[0])
    for _ in range(5):              # five one-minute checks
        SavantHostCheck(dev()).run()
        clock[0] += 60
    assert len(calls) == 1          # one SSH login in five minutes
    SavantHostCheck(dev()).run()
    assert len(calls) == 2


def test_port_down_skips_ssh(monkeypatch):
    calls = fake_ssh(monkeypatch, output())

    def refuse(h, p, t):
        raise OSError(61, "Connection refused")

    monkeypatch.setattr(savant, "tcp_connect", refuse)
    r = SavantHostCheck(dev()).run()
    assert r.status is Status.DOWN and calls == []


@pytest.mark.parametrize("stdout,ok", [("scheduled\n", True), ("no-permission\n", False)])
def test_reboot_action(monkeypatch, stdout, ok):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0 if ok else 3, stdout, "")

    monkeypatch.setattr("avfleet.actions.savant_reboot.subprocess.run", run)
    result = build_action("savant_reboot", dev(), {}).run()
    assert result.ok is ok
    assert "RPM@h" in seen["argv"] and "sudo -n /sbin/reboot" in seen["argv"][-1]


def test_separator_is_safe_in_zsh():
    # zsh expands a word starting with "=" as a command path and aborts the whole line.
    assert not SEP.startswith("=")
    assert f"echo '{SEP}'" in savant.INFO_SCRIPT


def test_remote_command_error_is_not_called_a_login_failure(monkeypatch):
    fake_ssh(monkeypatch, rc=1, stderr="zsh:1: something not found")
    r = SavantHostCheck(dev()).run()
    assert r.status is Status.DEGRADED and "status command failed" in r.detail


def test_own_command_line_in_ps_does_not_break_parsing():
    ps = PS_OK + f"RPM 2001 0.0 0.0 1 1 ? Ss 16:00 0:00 zsh -c echo '{SEP}'; uname -r; echo '{SEP}'\n"
    info = parse_info(output(ps))
    assert info["model"] == "PAV-SIPA1SM-00" and info["name"] == "Healing House Host"
    assert all(info["processes"].values())


def test_never_runs_racepointosver():
    # ~24 s of full CPU on the host per call: never part of a routine check.
    assert "racepointosver" not in savant.INFO_SCRIPT


def test_no_version_still_reads_system_running(monkeypatch):
    no_release = SEP + output().split(SEP, 1)[1]      # release-file section empty
    fake_ssh(monkeypatch, no_release)
    assert SavantHostCheck(dev()).run().detail == "System Running"


def test_timeout_message_is_readable(monkeypatch):
    def slow(self, script, timeout=20):
        raise subprocess.TimeoutExpired(["ssh", "long", "command"], 20)

    monkeypatch.setattr(SavantHostCheck, "run_ssh", slow)
    r = SavantHostCheck(dev()).run()
    assert r.status is Status.DEGRADED
    assert r.detail == "host answering; Savant status timed out after 20 s"


def test_release_channel_has_no_env_suffix():
    assert parse_info(output().replace("CLOUD_ENV=cbeta1", "CLOUD_ENV="))["version"] == "12.0:555"


def test_script_reads_files_not_credentials():
    assert "/etc/savant-os-release" in savant.INFO_SCRIPT and ".cloud_env_key" in savant.INFO_SCRIPT
    assert "scsCredentials" not in savant.INFO_SCRIPT
