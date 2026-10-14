"""avfleet as an MCP server: let Claude ask about the fleet and, when you allow it, act on it.

Transport is stdio (JSON-RPC 2.0, one message per line), so Claude Desktop can run it
locally or on the monitoring host over SSH: ``ssh mini avfleet mcp -c config.toml``.
Standard library only, like the rest of avfleet.

Tools come in two kinds:

* **Read tools** (always available, annotated read-only): fleet status, device detail and
  history with outage windows, recent events, Lutron keypad buttons.
* **Actions** (opt-in per kind in ``[mcp] actions``; default is maintenance only):
  - ``maintenance``  start / end maintenance windows
  - ``self_healing`` run a device's configured self-healing action (e.g. restart Roon), honouring its own
                     safety precondition and the configured cooldown
  - ``reboot``         reboot a device that has a ``[devices.reboot]`` action (TV, Roon app...)
  - ``lutron_buttons`` press a keypad button that exists in the processor's programming

Every action is written to the event log with "via Claude", so the dashboard shows who did
what. MCP clients such as Claude Desktop also ask the user to approve each tool call.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

from . import __version__, maintenance, reboot
from .actions import build_action
from .checks.lutron import connect_paired, discover_buttons, press_button
from .config import Config, DeviceConfig
from .models import Event
from .store import Store
from .web import build_status

DEFAULT_PROTOCOL = "2025-06-18"
READ = {"readOnlyHint": True, "openWorldHint": False}
ACT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


class ToolError(Exception):
    """A problem the user should see (bad device id, action not allowed...)."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    annotations: dict[str, Any] = field(default_factory=dict)
    action_kind: str | None = None  # None = read tool


def _ago(ts: float | None, now: float) -> str | None:
    if not ts:
        return None
    s = max(0, now - ts)
    if s < 90:
        return f"{int(s)}s ago"
    if s < 5400:
        return f"{int(s // 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def outages(rows: list[dict[str, Any]], interval_s: float) -> list[dict[str, Any]]:
    """Contiguous runs of DOWN results (outside maintenance) as outage windows."""
    windows, current = [], None
    for r in rows:
        down = r["status"] == "down" and not r["maintenance"]
        if down and current is None:
            current = {"start": r["ts"], "end": r["ts"], "checks": 1, "first_detail": r["detail"]}
        elif down:
            current["end"] = r["ts"]
            current["checks"] += 1
        elif current is not None:
            current["recovered"] = r["ts"]
            windows.append(current)
            current = None
    if current is not None:
        current["ongoing"] = True
        windows.append(current)
    for w in windows:
        end = w.get("recovered", w["end"] + interval_s)
        w["minutes"] = round((end - w["start"]) / 60, 1)
        w["start"] = _clock(w["start"])
        w["end"] = _clock(end) if not w.get("ongoing") else None
        w.pop("recovered", None)
    return windows


class FleetMCP:
    def __init__(self, config: Config, store: Store, clock: Callable[[], float] = time.time,
                 lutron_connect: Callable[..., Any] = connect_paired):
        self.config = config
        self.store = store
        self.clock = clock
        self.lutron_connect = lutron_connect
        self.tools = {t.name: t for t in self._tools() if t.action_kind is None or t.action_kind in config.mcp_actions}

    # --- helpers ----------------------------------------------------------------------

    def _device(self, device_id: str) -> DeviceConfig:
        for d in self.config.enabled_devices:
            if d.id == device_id:
                return d
        known = ", ".join(d.id for d in self.config.enabled_devices)
        raise ToolError(f"unknown device '{device_id}'. Known devices: {known}")

    def _lutron(self, device_id: str | None) -> DeviceConfig:
        candidates = [d for d in self.config.enabled_devices if d.type == "lutron_leap"]
        if device_id:
            dev = self._device(device_id)
            if dev.type != "lutron_leap":
                raise ToolError(f"'{device_id}' is not a Lutron processor")
            return dev
        from .checks.lutron import find_pairing

        paired = [d for d in candidates if find_pairing(d.host)]
        if not paired:
            raise ToolError("no paired Lutron processor (run lap-pair first)")
        return paired[0]

    def _log(self, device_id: str, kind: str, message: str) -> None:
        self.store.add_event(Event(device_id, kind, message, timestamp=self.clock()))

    # --- read tools -------------------------------------------------------------------

    def fleet_status(self, _args):
        now = self.clock()
        status = build_status(self.config, self.store, now=now)
        devices = [{
            "id": d["id"], "name": d["name"], "group": d["group"], "status": d["status"],
            "detail": d["detail"], "latency_ms": d["latency_ms"], "typical_latency_ms": d["latency_typical_ms"],
            "uptime_24h_pct": d["uptime_24h"],
            "status_since": _ago(d["since"], now), "last_checked": _ago(d["checked_at"], now),
            "maintenance": ({"until": _clock(d["maintenance"]["until"]), "reason": d["maintenance"]["reason"]}
                            if d["maintenance"] else None),
            "notice": (d["metrics"] or {}).get("notice"),
        } for d in status["devices"]]
        counts: dict[str, int] = {}
        for d in devices:
            key = "maintenance" if d["maintenance"] else d["status"]
            counts[key] = counts.get(key, 0) + 1
        return {"summary": counts, "devices": devices}

    def device_detail(self, args):
        dev = self._device(args["device"])
        now = self.clock()
        latest = self.store.latest(dev.id)
        hours = float(args.get("hours", 24))
        rows = self.store.results_since(dev.id, now - hours * 3600)
        return {
            "id": dev.id, "name": dev.name, "type": dev.type, "group": dev.group, "host": dev.host,
            "status": latest["status"] if latest else "unknown",
            "detail": latest["detail"] if latest else None,
            "metrics": latest["metrics"] if latest else {},
            "last_checked": _ago(latest["ts"], now) if latest else None,
            "status_since": _ago(self.store.last_change(dev.id), now),
            "typical_latency_ms": self.store.latency_typical(dev.id, now=now),
            "uptime_pct": self.store.uptime(dev.id, window_s=hours * 3600, now=now),
            "window_hours": hours,
            "outages": outages(rows, self.config.interval_s),
            "self_healing": dev.remediation.action if dev.remediation else None,
            "maintenance": self.store.active_maintenance(now).get(dev.id),
            "recent_events": [{"when": _clock(e["ts"]), "kind": e["kind"], "message": e["message"]}
                              for e in self.store.events_for(dev.id, limit=15)],
        }

    def recent_events(self, args):
        limit = max(1, min(200, int(args.get("limit", 25))))
        since = self.clock() - float(args["hours"]) * 3600 if args.get("hours") else None
        if args.get("device"):
            self._device(args["device"])
        events = self.store.events_for(args.get("device"), args.get("kind"), limit=limit, since=since)
        return [{"when": _clock(e["ts"]), "device": e["device_id"], "kind": e["kind"], "message": e["message"]}
                for e in events]

    def list_keypad_buttons(self, args):
        dev = self._lutron(args.get("device"))
        client = self.lutron_connect(dev.host)
        try:
            return {"processor": dev.id, "buttons": discover_buttons(client)}
        finally:
            client.close()

    # --- actions ----------------------------------------------------------------------

    def start_maintenance(self, args):
        names = [args["device"]] if args["device"] != "all" else ["all"]
        minutes = int(args.get("minutes", 30))
        if not 1 <= minutes <= 7 * 24 * 60:
            raise ToolError("minutes must be between 1 and 10080")
        reason = (args.get("reason") or "")[:120]
        try:
            ids = maintenance.start(self.store, self.config, names, minutes * 60, reason, now=self.clock(),
                                    via="Claude")
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"started": ids, "minutes": minutes, "until": _clock(self.clock() + minutes * 60), "reason": reason}

    def end_maintenance(self, args):
        try:
            ended = maintenance.end(self.store, self.config, [args.get("device") or "all"], now=self.clock(),
                                    via="Claude")
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"ended": ended} if ended else {"ended": [], "note": "nothing was in maintenance"}

    def run_self_healing(self, args):
        dev = self._device(args["device"])
        if not dev.remediation:
            raise ToolError(f"{dev.name} has no self-healing action configured")
        cfg = dev.remediation
        now = self.clock()
        recent = self.store.events_for(dev.id, kind="action", limit=1, since=now - cfg.cooldown_s)
        if recent:
            wait = int(cfg.cooldown_s - (now - recent[0]["ts"])) // 60 + 1
            raise ToolError(f"{cfg.action} ran {_ago(recent[0]['ts'], now)}; cooldown has ~{wait} min left")
        outcome = build_action(cfg.action, dev, cfg.options).run()
        self._log(dev.id, "action", f"{dev.name}: {cfg.action} via Claude -> {outcome.message}")
        if not outcome.ok:
            raise ToolError(outcome.message)
        return {"device": dev.id, "action": cfg.action, "result": outcome.message}

    def reboot_device(self, args):
        try:
            outcome = reboot.run(self.store, self.config, args["device"], via="Claude", now=self.clock())
        except reboot.RebootError as exc:
            raise ToolError(str(exc)) from None
        if not outcome.ok:
            raise ToolError(outcome.message)
        return {"device": args["device"], "result": outcome.message}

    def press_keypad_button(self, args):
        dev = self._lutron(args.get("device"))
        button = str(args["button"]).strip()
        client = self.lutron_connect(dev.host)
        try:
            known = {b["button"]: b for b in discover_buttons(client)}
            if button not in known:
                raise ToolError(f"button {button} isn't a keypad button in this system; use list_keypad_buttons")
            press_button(client, button)
        finally:
            client.close()
        b = known[button]
        self._log(dev.id, "action", f"{dev.name}: pressed '{b['label']}' on {b['keypad']} ({b['area']}) via Claude")
        return {"pressed": b}

    # --- tool table -------------------------------------------------------------------

    def _tools(self) -> list[Tool]:
        device = {"type": "string", "description": "Device id from fleet_status, e.g. 'roon'"}
        return [
            Tool("fleet_status", "Current health of every monitored AV and network device: status (up, degraded, "
                 "down, unknown), detail, response time, 24-hour uptime, maintenance windows and notices.",
                 {"type": "object", "properties": {}}, self.fleet_status, READ),
            Tool("device_detail", "Everything known about one device: latest status and metrics, uptime and "
                 "outage windows over a period (default 24 h), self-healing, maintenance and its recent events. "
                 "Use this to explain why something went down.",
                 {"type": "object", "properties": {"device": device, "hours": {
                     "type": "number", "description": "History window in hours (default 24, max 168)",
                     "minimum": 1, "maximum": 168}}, "required": ["device"]},
                 self.device_detail, READ),
            Tool("recent_events", "The event log: state changes, alerts, self-healing actions and maintenance, "
                 "newest first. Filter by device, kind or time window.",
                 {"type": "object", "properties": {
                     "device": device,
                     "kind": {"type": "string", "enum": ["state", "alert", "action", "maintenance", "info"]},
                     "hours": {"type": "number", "minimum": 1, "maximum": 168},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
                 self.recent_events, READ),
            Tool("list_keypad_buttons", "Lutron keypad buttons programmed in the processor (physical, homeowner and "
                 "phantom keypads), with their engraving, keypad and room.",
                 {"type": "object", "properties": {"device": {
                     "type": "string", "description": "Lutron processor device id (default: the paired one)"}}},
                 self.list_keypad_buttons, READ),
            Tool("start_maintenance", "Put a device (or 'all') into maintenance: it keeps being checked but alerts "
                 "and self-healing pause until the window ends. Confirm the device and duration with the user first.",
                 {"type": "object", "properties": {
                     "device": {"type": "string", "description": "Device id, or 'all'"},
                     "minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 30},
                     "reason": {"type": "string", "maxLength": 120}}, "required": ["device"]},
                 self.start_maintenance, ACT, "maintenance"),
            Tool("end_maintenance", "End maintenance now for a device (or all devices if none given).",
                 {"type": "object", "properties": {"device": {"type": "string"}}},
                 self.end_maintenance, ACT, "maintenance"),
            Tool("run_self_healing", "Run a device's configured self-healing action now (e.g. restart Roon). "
                 "It still honours the action's safety checks and cooldown. Confirm with the user first.",
                 {"type": "object", "properties": {"device": device}, "required": ["device"]},
                 self.run_self_healing, ACT, "self_healing"),
            Tool("reboot_device", "Reboot or restart a device that has a reboot action configured (e.g. the TV, "
                 "or the Roon app). Has a cooldown and the action's own safety checks. The device will be "
                 "unavailable briefly. Confirm with the user first.",
                 {"type": "object", "properties": {"device": device}, "required": ["device"]},
                 self.reboot_device, ACT, "reboot"),
            Tool("press_keypad_button", "Press a Lutron keypad button (e.g. start a Natural Show). Only buttons that "
                 "exist in the processor's programming can be pressed; get ids from list_keypad_buttons. Confirm "
                 "with the user first.",
                 {"type": "object", "properties": {
                     "button": {"type": "string", "description": "Button id from list_keypad_buttons"},
                     "device": {"type": "string"}}, "required": ["button"]},
                 self.press_keypad_button, ACT, "lutron_buttons"),
        ]

    # --- JSON-RPC ---------------------------------------------------------------------

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification (e.g. notifications/initialized): never answered
            return None
        try:
            if method == "initialize":
                version = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "avfleet", "version": __version__},
                    "instructions": "Monitors a home AV and network fleet. Read tools are safe to call freely. "
                                    "Judge a device's response time against its own typical_latency_ms, not "
                                    "against other devices: protocols differ (an encrypted Lutron session is "
                                    "normally slower than a plain HTTP status call). Before calling any action "
                                    "tool, state what you'll do and get the user's OK.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema,
                                     "annotations": t.annotations} for t in self.tools.values()]}
            elif method == "tools/call":
                params = msg.get("params") or {}
                tool = self.tools.get(params.get("name"))
                if tool is None:
                    return _error(mid, -32602, f"unknown tool: {params.get('name')}")
                result = self._call(tool, params.get("arguments") or {})
            else:
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 - protocol errors must not kill the server
            return _error(mid, -32603, f"internal error: {exc}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _call(self, tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
        result = self._run(tool, args)
        try:
            self.store.record_mcp(tool.name, not result["isError"], now=self.clock())
        except Exception:  # noqa: BLE001 - activity tracking must never break a tool call
            pass
        return result

    def _run(self, tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = tool.handler(args)
            return {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}], "isError": False}
        except (ToolError, KeyError) as exc:
            text = f"missing argument {exc}" if isinstance(exc, KeyError) else str(exc)
            return {"content": [{"type": "text", "text": text}], "isError": True}
        except Exception as exc:  # noqa: BLE001 - device/network errors become readable tool errors
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}

    def serve(self, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = _error(None, -32700, "parse error")
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "invalid request")
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
