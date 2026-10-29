from conftest import device

from avfleet.checks import build_check
from avfleet.checks.kaleidescape import build_command, parse_reply
from avfleet.models import Status


def test_tcp_check_open_and_closed(fake_tcp, closed_port):
    srv = fake_tcp(lambda data: b"")
    assert build_check(device("tcp", port=srv.port)).run().status is Status.UP
    r = build_check(device("tcp", ports=[srv.port, closed_port])).run()
    assert r.status is Status.DOWN and str(closed_port) in r.detail


def test_tcp_check_needs_port():
    assert build_check(device("tcp")).run().status is Status.UNKNOWN


def test_roon_core_up_and_down(fake_tcp, closed_port):
    srv = fake_tcp(lambda data: b"")
    up = build_check(device("roon", port=srv.port)).run()
    assert up.status is Status.UP and up.latency_ms is not None
    down = build_check(device("roon", port=closed_port)).run()
    assert down.status is Status.DOWN and "not accepting" in down.detail


# --- Kaleidescape protocol ----------------------------------------------------------

def test_build_command():
    assert build_command("01", 3, "GET_DEVICE_POWER_STATE") == b"01/3/GET_DEVICE_POWER_STATE:\r"


def test_parse_reply_with_checksum():
    r = parse_reply("01/1/000:DEVICE_POWER_STATE:1:1:/89")
    assert (r.device, r.seq, r.status, r.name, r.fields) == ("01", "1", 0, "DEVICE_POWER_STATE", ["1", "1"])


def test_parse_reply_without_checksum_and_garbage():
    assert parse_reply("01/!/000:PLAY_STATUS:0:0:").name == "PLAY_STATUS"
    assert parse_reply("hello") is None
    assert parse_reply("01/1/xyz:FOO:") is None


def kscape_responder(power="1", status="000", noise=True):
    def respond(data: bytes) -> bytes:
        if b"GET_DEVICE_POWER_STATE" not in data:
            return b""
        out = b"01/!/000:PLAY_STATUS:2:0:01:00000:00000:000:00000:00000:/12\r" if noise else b""
        return out + f"01/1/{status}:DEVICE_POWER_STATE:{power}:1:/89\r".encode()
    return respond


def test_kaleidescape_on_ignores_unsolicited_events(fake_tcp):
    srv = fake_tcp(kscape_responder(power="1"))
    r = build_check(device("kaleidescape", port=srv.port)).run()
    assert r.status is Status.UP and r.detail == "On"


def test_kaleidescape_standby(fake_tcp):
    srv = fake_tcp(kscape_responder(power="0", noise=False))
    assert build_check(device("kaleidescape", port=srv.port)).run().detail == "Standby"


def test_kaleidescape_error_status_degraded(fake_tcp):
    srv = fake_tcp(kscape_responder(status="020"))
    assert build_check(device("kaleidescape", port=srv.port)).run().status is Status.DEGRADED


def test_kaleidescape_silent_device_down(fake_tcp):
    srv = fake_tcp(lambda data: b"")
    dev = device("kaleidescape", port=srv.port)
    dev.timeout_s = 0.5
    assert build_check(dev).run().status is Status.DOWN


def test_lutron_unreachable_down(closed_port):
    assert build_check(device("lutron_leap", port=closed_port)).run().status is Status.DOWN


def test_title_state_card_style():
    from avfleet.checks.base import title_state
    assert title_state("standby") == "Standby"
    assert title_state("optical1") == "Optical 1"
    assert title_state("net_radio") == "Net Radio"
