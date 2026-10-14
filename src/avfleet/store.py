"""SQLite history: every check result and every event, kept for ``history_days``.

The dashboard reads only from here, so the web server and the monitor loop never share
in-memory state and the dashboard keeps working if the loop stalls (it just shows stale data).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import CheckResult, Event, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    ts REAL NOT NULL, device_id TEXT NOT NULL, status TEXT NOT NULL,
    latency_ms REAL, detail TEXT, metrics TEXT
);
CREATE INDEX IF NOT EXISTS results_dev_ts ON results(device_id, ts);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL, device_id TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS maintenance (
    device_id TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '', ended_early INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS maintenance_dev ON maintenance(device_id, end_ts);
"""

HEALTHY = (Status.UP.value, Status.DEGRADED.value)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")

    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(results)")}
        if "maintenance" not in cols:
            self._conn.execute("ALTER TABLE results ADD COLUMN maintenance INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- writes ---------------------------------------------------------------------

    def add_result(self, r: CheckResult, maintenance: bool = False) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO results (ts, device_id, status, latency_ms, detail, metrics, maintenance) "
                "VALUES (?,?,?,?,?,?,?)",
                (r.timestamp, r.device_id, r.status.value, r.latency_ms, r.detail, json.dumps(r.metrics),
                 int(maintenance)),
            )

    # --- maintenance windows ------------------------------------------------------------

    def start_maintenance(self, device_id: str, until: float, reason: str = "", now: float | None = None) -> None:
        """Open a window (replacing any active one for this device)."""
        now = now or time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE maintenance SET end_ts=?, ended_early=1 WHERE device_id=? AND end_ts>? AND start_ts<=?",
                (now, device_id, now, now),
            )
            self._conn.execute("INSERT INTO maintenance (device_id, start_ts, end_ts, reason) VALUES (?,?,?,?)",
                               (device_id, now, until, reason))

    def end_maintenance(self, device_id: str, now: float | None = None) -> bool:
        """Close the device's active window early. Returns False if there wasn't one."""
        now = now or time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE maintenance SET end_ts=?, ended_early=1 WHERE device_id=? AND end_ts>? AND start_ts<=?",
                (now, device_id, now, now),
            )
            return cur.rowcount > 0

    def active_maintenance(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """{device_id: {"started", "until", "reason"}} for windows open right now."""
        now = now or time.time()
        rows = self._q("SELECT * FROM maintenance WHERE start_ts<=? AND end_ts>? ORDER BY start_ts", (now, now))
        return {r["device_id"]: {"started": r["start_ts"], "until": r["end_ts"], "reason": r["reason"]} for r in rows}

    def last_maintenance(self, device_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM maintenance WHERE device_id=? ORDER BY start_ts DESC LIMIT 1", (device_id,))
        return dict(rows[0]) if rows else None

    def add_event(self, e: Event) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO events VALUES (?,?,?,?)", (e.timestamp, e.device_id, e.kind, e.message))

    def prune(self, keep_days: int, now: float | None = None) -> None:
        cutoff = (now or time.time()) - keep_days * 86400
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM results WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM maintenance WHERE end_ts < ?", (cutoff,))

    # --- reads ----------------------------------------------------------------------

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def latest(self, device_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM results WHERE device_id=? ORDER BY ts DESC LIMIT 1", (device_id,))
        if not rows:
            return None
        row = dict(rows[0])
        row["metrics"] = json.loads(row["metrics"] or "{}")
        return row

    def uptime(self, device_id: str, window_s: float = 86400, now: float | None = None) -> float | None:
        """Share of checks in the window that were UP or DEGRADED, 0..100. None if no data."""
        since = (now or time.time()) - window_s
        rows = self._q(
            "SELECT SUM(CASE WHEN status IN (?,?) THEN 1 ELSE 0 END) AS ok, COUNT(*) AS n "
            "FROM results WHERE device_id=? AND ts>=? AND status != ? AND maintenance = 0",
            (*HEALTHY, device_id, since, Status.UNKNOWN.value),
        )
        ok, n = rows[0]["ok"], rows[0]["n"]
        return round(100.0 * ok / n, 2) if n else None

    def latency_series(self, device_id: str, window_s: float = 86400, points: int = 48,
                       now: float | None = None) -> list[float | None]:
        """Average latency per bucket across the window (None where no successful checks)."""
        now = now or time.time()
        since = now - window_s
        bucket = window_s / points
        rows = self._q(
            "SELECT CAST((ts-?)/? AS INTEGER) AS b, AVG(latency_ms) AS avg FROM results "
            "WHERE device_id=? AND ts>=? AND latency_ms IS NOT NULL GROUP BY b",
            (since, bucket, device_id, since),
        )
        series: list[float | None] = [None] * points
        for r in rows:
            if 0 <= r["b"] < points:
                series[r["b"]] = round(r["avg"], 1)
        return series

    def last_change(self, device_id: str) -> float | None:
        """Timestamp when the device entered its current status."""
        latest = self.latest(device_id)
        if not latest:
            return None
        rows = self._q(
            "SELECT MAX(ts) AS ts FROM results WHERE device_id=? AND status != ?",
            (device_id, latest["status"]),
        )
        prev = rows[0]["ts"]
        if prev is None:
            first = self._q("SELECT MIN(ts) AS ts FROM results WHERE device_id=?", (device_id,))
            return first[0]["ts"]
        nxt = self._q("SELECT MIN(ts) AS ts FROM results WHERE device_id=? AND ts > ?", (device_id, prev))
        return nxt[0]["ts"]

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))]
