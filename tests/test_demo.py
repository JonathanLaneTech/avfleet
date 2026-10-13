"""End-to-end: real checks against protocol simulators, including a crash and self-heal."""

from avfleet.demo import ROON, build_demo
from avfleet.models import Status


def test_demo_fleet_healthy_then_self_heals():
    config, store, monitor = build_demo(port=0)
    results = {r.device_id: r for r in monitor.run_once()}
    assert {r.status for r in results.values()} == {Status.UP}, {k: r.detail for k, r in results.items()}
    assert results["switch"].metrics["ports_up"] == 11
    assert results["apple-tv"].metrics["model"] == "AppleTV14,1"

    ROON["sim"].crash()
    for _ in range(3):
        monitor.run_once()
    messages = [e["message"] for e in store.events()]
    assert any("Roon Core is DOWN" in m for m in messages)
    assert any("demo_roon_restart -> restarted Roon (simulated)" in m for m in messages)

    roon = next(r for r in monitor.run_once() if r.device_id == "roon")
    assert roon.status is Status.UP
    assert any("Roon Core recovered" in e["message"] for e in store.events())
