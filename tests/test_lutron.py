"""Paired LEAP: a fake QSX shaped like a real HomeWorks QSX project (identifiers made up)."""

import json
import socket
import threading

import pytest
from conftest import device

from avfleet.checks import build_check
from avfleet.checks.lutron import LeapClient, LeapError, LutronDevice, find_pairing, summarize, walk
from avfleet.models import Status

OK = "200 OK"
IDLE = {"Availability": "Unknown", "ConfigurationTransferStatus": "NotRequired"}


def qsx_routes(availability="Unknown", transfer="NotRequired", level=100):
    return {
        "/server": {"Servers": [{"href": "/server/1", "Type": "LEAP"}]},
        "/project": {"Project": {"Name": "Example Home", "MasterDeviceList": {
            "Devices": [{"href": "/device/575"}, {"href": "/device/587"}]}}},
        "/device/575": {"Device": {"Name": "Enclosure Device", "DeviceType": "HWQSProcessor", "ModelNumber": "HQP7-2",
                                   "IsThisDevice": True, "OwnedLinks": [{"href": "/link/577"}, {"href": "/link/579"}],
                                   "FirmwareImage": {"Firmware": {"DisplayName": "26.07.34f000"}}}},
        "/device/587": {"Device": {"Name": "Gateway X", "DeviceType": "HWQSProcessor", "ModelNumber": "HQP7-RF",
                                   "OwnedLinks": [{"href": "/link/589"}]}},
        "/device/555": {"Device": {"Name": "1", "DeviceType": "KetraA20", "ModelNumber": "HW-A20",
                                   "LocalZones": [{"href": "/zone/554"}]}},
        "/link/577/associatedlinknode": {"LinkNodes": []},
        "/link/579/associatedlinknode": {"LinkNodes": []},
        "/link/589/associatedlinknode": {"LinkNodes": [
            {"href": "/device/555/linknode/560", "Parent": {"href": "/device/555"}},
            {"href": "/device/587/linknode/588", "Parent": {"href": "/device/587"}}]},
        "/device/575/status": {"DeviceStatus": IDLE},
        "/device/587/status": {"DeviceStatus": IDLE},
        "/device/555/status": {"DeviceStatus": {"Availability": availability, "ConfigurationTransferStatus": transfer}},
        "/zone/554": {"Zone": {"Name": "Lamp"}},
        "/zone/554/status": {"ZoneStatus": {"Level": level,
                                            "ColorTuningStatus": {"WhiteTuningLevel": {"Kelvin": 3500}}}},
    }


class FakeLeap:
    """Line-delimited JSON LEAP server over plain TCP (TLS is the transport's concern, not the protocol's)."""

    def __init__(self, routes, noise=False, silent=False):
        self.routes, self.requested = routes, []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]

        def serve():
            conn, _ = self.srv.accept()
            buf = b""
            with conn:
                while True:
                    try:
                        data = conn.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    buf += data
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        req = json.loads(line)
                        url, tag = req["Header"]["Url"], req["Header"].get("ClientTag")
                        self.requested.append(url)
                        if silent:
                            continue
                        out = b""
                        if noise:
                            out += json.dumps({"CommuniqueType": "ReadResponse",
                                               "Header": {"Url": "/event", "StatusCode": OK}}).encode() + b"\r\n"
                        if url in routes:
                            reply = {"Header": {"StatusCode": OK, "ClientTag": tag}, "Body": routes[url]}
                        else:
                            reply = {"Header": {"StatusCode": "400 BadRequest", "ClientTag": tag},
                                     "Body": {"Message": "This request is not supported"}}
                        conn.sendall(out + json.dumps(reply).encode() + b"\r\n")

        threading.Thread(target=serve, daemon=True).start()

    def client(self, timeout=2.0):
        return LeapClient(socket.create_connection(("127.0.0.1", self.port)), timeout)

    def close(self):
        self.srv.close()


@pytest.fixture
def qsx():
    servers = []

    def make(**kw):
        noise = kw.pop("noise", False)
        silent = kw.pop("silent", False)
        s = FakeLeap(qsx_routes(**kw), noise=noise, silent=silent)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def test_walk_finds_ketra_through_gateway_link(qsx):
    srv = qsx()
    topo = walk(srv.client())
    assert topo["project"] == "Example Home"
    assert topo["processor"].model == "HQP7-2" and topo["processor"].firmware == "26.07.34f000"
    ketra = [d for d in topo["devices"] if d.is_ketra]
    assert len(ketra) == 1 and ketra[0].label == "Lamp"
    assert ketra[0].zones[0] == {"href": "/zone/554", "name": "Lamp", "level": 100, "kelvin": 3500}


def test_walk_never_reads_link_records(qsx):
    """/link/<id> carries the Clear Connect X network key; the walk must not request it."""
    srv = qsx()
    walk(srv.client())
    assert not [u for u in srv.requested if u.startswith("/link/") and not u.endswith("/associatedlinknode")]


def test_summary_healthy_single_zone(qsx):
    status, detail, m = summarize(walk(qsx().client()))
    assert status is Status.UP
    assert detail == "1 Ketra fixture · Lamp set to 100% · 3500 K"
    assert m["firmware"] == "26.07.34f000" and m["ketra_total"] == 1 and m["zones_on"] == 1
    assert m["availability_unknown"] == 1  # processor not reporting availability yet: shown, not failed


def test_summary_off(qsx):
    _, detail, m = summarize(walk(qsx(level=0).client()))
    assert detail.endswith("Lamp set off") and m["zones_on"] == 0


def test_unavailable_fixture_is_degraded_and_named(qsx):
    status, detail, m = summarize(walk(qsx(availability="Unavailable").client()))
    assert status is Status.DEGRADED
    assert detail == "1 unavailable: Lamp"
    assert m["ketra_unavailable"] == 1 and m["unavailable"] == ["Lamp"]


def test_pending_programming_transfer_is_degraded(qsx):
    status, detail, _ = summarize(walk(qsx(availability="Available", transfer="InProgress").client()))
    assert status is Status.DEGRADED and "transfer inprogress" in detail


def test_client_skips_unsolicited_messages(qsx):
    srv = qsx(noise=True)
    assert walk(srv.client())["processor"] is not None


def test_client_times_out(qsx):
    srv = qsx(silent=True)
    with pytest.raises(LeapError, match="no reply"):
        srv.client(timeout=0.3).body("/server")


def test_body_raises_on_unsupported(qsx):
    with pytest.raises(LeapError, match="400"):
        qsx().client().body("/zone/status/expanded")


def test_many_fixtures_summary():
    devs = [LutronDevice(f"/device/{i}", str(i), "KetraA20", availability="Available",
                         zones=[{"name": f"Z{i}", "level": 50 if i % 2 else 0}]) for i in range(6)]
    devs[2].availability = "Unavailable"
    status, detail, m = summarize({"processor": None, "devices": devs})
    assert status is Status.DEGRADED and detail == "1 unavailable: Z2"
    devs[2].availability = "Available"
    status, detail, m = summarize({"processor": None, "devices": devs})
    assert status is Status.UP and detail == "6 Ketra fixtures · 3/6 zones on"
    assert "availability_unknown" not in m


def test_find_pairing(tmp_path):
    assert find_pairing("192.0.2.40", tmp_path) is None
    for suffix in (".crt", ".key", "-bridge.crt"):
        (tmp_path / f"192.0.2.40{suffix}").write_text("x")
    cert, key, ca = find_pairing("192.0.2.40", tmp_path)
    assert cert.endswith(".crt") and key.endswith(".key") and ca.endswith("-bridge.crt")


def test_unpaired_check_uses_handshake_and_reports_down_when_unreachable(closed_port, monkeypatch, tmp_path):
    monkeypatch.setattr("avfleet.checks.lutron.PAIRING_DIR", tmp_path)
    r = build_check(device("lutron_leap", port=closed_port)).run()
    assert r.status is Status.DOWN
