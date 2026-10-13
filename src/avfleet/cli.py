"""Command line: ``avfleet run | check | types``."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .checks import build_check, check_types
from .config import ConfigError, load_config
from .engine import Monitor
from .models import Status
from .notifiers import build_notifiers
from .store import Store
from .web import start_server


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_run(args) -> int:
    config = load_config(args.config)
    store = Store(config.db_path)
    monitor = Monitor(config, store, build_notifiers(config.notifiers), dry_run=args.dry_run)
    if not args.no_web:
        start_server(config, store)
    logging.getLogger("avfleet").info("monitoring %d devices every %ss%s", len(monitor.states),
                                      config.interval_s, " (dry run)" if args.dry_run else "")
    try:
        monitor.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_check(args) -> int:
    """One pass over every device, printed as a table. Exit 1 if anything is DOWN (cron/CI friendly)."""
    config = load_config(args.config)
    devices = [d for d in config.enabled_devices if not args.device or d.id in args.device]
    worst = 0
    print(f"{'DEVICE':<22} {'STATUS':<9} {'MS':>7}  DETAIL")
    for dev in devices:
        r = build_check(dev).run()
        ms = f"{r.latency_ms:.0f}" if r.latency_ms is not None else "-"
        print(f"{dev.id:<22} {r.status.value:<9} {ms:>7}  {r.detail}")
        if r.status is Status.DOWN:
            worst = 1
    return worst


def cmd_demo(args) -> int:
    from .demo import run_demo

    run_demo(port=args.port, crash_after=args.crash_after)
    return 0


def cmd_types(_args) -> int:
    print("\n".join(check_types()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="avfleet", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="monitor continuously and serve the dashboard")
    run.add_argument("-c", "--config", default="config.toml")
    run.add_argument("--dry-run", action="store_true", help="log self-healing actions without running them")
    run.add_argument("--no-web", action="store_true", help="don't start the dashboard")
    run.set_defaults(func=cmd_run)

    chk = sub.add_parser("check", help="check every device once and print the results")
    chk.add_argument("-c", "--config", default="config.toml")
    chk.add_argument("device", nargs="*", help="limit to these device ids")
    chk.set_defaults(func=cmd_check)

    demo = sub.add_parser("demo", help="run the monitor and dashboard against simulated devices")
    demo.add_argument("--port", type=int, default=8080)
    demo.add_argument("--crash-after", type=int, default=30, help="seconds before the simulated Roon crashes")
    demo.set_defaults(func=cmd_demo)

    typ = sub.add_parser("types", help="list available check types")
    typ.set_defaults(func=cmd_types)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
