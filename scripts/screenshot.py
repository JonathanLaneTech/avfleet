"""Regenerate docs/dashboard*.png from the demo fleet. Requires: pip install playwright."""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from avfleet.demo import ROON, build_demo
from avfleet.web import start_server

OUT = Path(__file__).resolve().parent.parent / "docs"


def main(executable: str | None = None) -> None:
    config, store, monitor = build_demo(port=8099)
    httpd = start_server(config, store)
    monitor.run_once()
    ROON["sim"].crash()
    for _ in range(3):
        monitor.run_once()
    monitor.run_once()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable) if executable else p.chromium.launch()
        for scheme, name in (("light", "dashboard.png"), ("dark", "dashboard-dark.png")):
            page = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme,
                                    device_scale_factor=2)
            page.goto(url)
            page.wait_for_selector(".tile")
            time.sleep(0.5)
            page.screenshot(path=str(OUT / name), full_page=True)
            page.close()
        browser.close()
    httpd.shutdown()
    print("wrote", OUT / "dashboard.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
