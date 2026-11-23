"""SQLite history store: scans, repairs, recoveries and settings.

Deliberately dependency-free (stdlib sqlite3) and stored outside the repository
in the per-user application-data directory.  Device serial numbers are stored
hashed by default, matching the privacy policy in the audit log.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from usbdoctor.core.logging import default_state_dir
from usbdoctor.core.util import iso_now
from usbdoctor.version import DB_SCHEMA_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    device_path  TEXT NOT NULL,
    device_name  TEXT,
    serial_hash  TEXT,
    size_bytes   INTEGER,
    verdict      TEXT,
    confidence   TEXT,
    report_json  TEXT,
    mode         TEXT
);
CREATE INDEX IF NOT EXISTS idx_scans_device ON scans(device_path, started_at);

CREATE TABLE IF NOT EXISTS repairs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id      INTEGER REFERENCES scans(id) ON DELETE SET NULL,
    started_at   TEXT NOT NULL,
    device_path  TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    title        TEXT,
    risk         TEXT,
    status       TEXT,
    verified     INTEGER,
    message      TEXT,
    duration_s   REAL,
    rollback_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_repairs_device ON repairs(device_path, started_at);

CREATE TABLE IF NOT EXISTS recoveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    device_path  TEXT NOT NULL,
    method       TEXT,
    output_dir   TEXT,
    files        INTEGER,
    bytes        INTEGER,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class ScanRecord:
    id: int
    started_at: str
    device_path: str
    device_name: str
    verdict: str
    confidence: str
    mode: str


class HistoryStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (default_state_dir() / "history.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialise()

    # ------------------------------------------------------------------ core
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialise(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(DB_SCHEMA_VERSION),),
            )

    # ----------------------------------------------------------------- scans
    def start_scan(
        self,
        *,
        device_path: str,
        device_name: str,
        serial_hash: Optional[str],
        size_bytes: Optional[int],
        mode: str,
    ) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO scans(started_at, device_path, device_name, serial_hash, size_bytes, mode) "
                "VALUES(?,?,?,?,?,?)",
                (iso_now(), device_path, device_name, serial_hash, size_bytes, mode),
            )
            return int(cursor.lastrowid)

    def finish_scan(self, scan_id: int, *, verdict: str, confidence: str, report: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE scans SET finished_at=?, verdict=?, confidence=?, report_json=? WHERE id=?",
                (iso_now(), verdict, confidence, json.dumps(report, default=str), scan_id),
            )

    def get_scan(self, scan_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row is None:
            return None
        payload = dict(row)
        if payload.get("report_json"):
            try:
                payload["report"] = json.loads(payload["report_json"])
            except json.JSONDecodeError:
                payload["report"] = None
        return payload

    def recent_scans(self, limit: int = 50, device_path: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT id, started_at, finished_at, device_path, device_name, verdict, confidence, mode, size_bytes FROM scans"
        params: tuple[Any, ...] = ()
        if device_path:
            query += " WHERE device_path = ?"
            params = (device_path,)
        query += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def last_scan_for(self, device_path: str) -> Optional[dict[str, Any]]:
        rows = self.recent_scans(limit=1, device_path=device_path)
        return rows[0] if rows else None

    # --------------------------------------------------------------- repairs
    def record_repair(
        self,
        *,
        scan_id: Optional[int],
        device_path: str,
        operation_id: str,
        title: str,
        risk: str,
        status: str,
        verified: Optional[bool],
        message: str,
        duration_s: float,
        rollback_file: Optional[str] = None,
    ) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO repairs(scan_id, started_at, device_path, operation_id, title, risk, "
                "status, verified, message, duration_s, rollback_file) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    iso_now(),
                    device_path,
                    operation_id,
                    title,
                    risk,
                    status,
                    1 if verified else 0 if verified is False else None,
                    message,
                    duration_s,
                    rollback_file,
                ),
            )
            return int(cursor.lastrowid)

    def recent_repairs(self, limit: int = 100, device_path: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM repairs"
        params: tuple[Any, ...] = ()
        if device_path:
            query += " WHERE device_path = ?"
            params = (device_path,)
        query += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def repair_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT operation_id, COUNT(*) AS n FROM repairs WHERE status='succeeded' GROUP BY operation_id"
            ).fetchall()
        return {row["operation_id"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------ recoveries
    def record_recovery(
        self,
        *,
        device_path: str,
        method: str,
        output_dir: str,
        files: int,
        bytes_written: int,
        details: dict[str, Any],
    ) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO recoveries(started_at, device_path, method, output_dir, files, bytes, details_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (iso_now(), device_path, method, output_dir, files, bytes_written, json.dumps(details, default=str)),
            )
            return int(cursor.lastrowid)

    def recent_recoveries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM recoveries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- settings
    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value))
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def all_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        output: dict[str, Any] = {}
        for row in rows:
            try:
                output[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                output[row["key"]] = row["value"]
        return output

    # ----------------------------------------------------------- statistics
    def statistics(self) -> dict[str, Any]:
        with self._connect() as conn:
            scans = conn.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"]
            repairs = conn.execute("SELECT COUNT(*) AS n FROM repairs").fetchone()["n"]
            succeeded = conn.execute(
                "SELECT COUNT(*) AS n FROM repairs WHERE status='succeeded'"
            ).fetchone()["n"]
            recovered = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(files),0) AS f FROM recoveries").fetchone()
            verdicts = conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM scans WHERE verdict IS NOT NULL GROUP BY verdict"
            ).fetchall()
        return {
            "scans": int(scans),
            "repairs": int(repairs),
            "repairs_succeeded": int(succeeded),
            "recovery_sessions": int(recovered["n"]),
            "files_recovered": int(recovered["f"]),
            "verdicts": {row["verdict"]: int(row["n"]) for row in verdicts},
        }

    def purge_device(self, device_path: str) -> None:
        """Delete every record about one device (privacy / 'forget this drive')."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM repairs WHERE device_path=?", (device_path,))
            conn.execute("DELETE FROM scans WHERE device_path=?", (device_path,))
            conn.execute("DELETE FROM recoveries WHERE device_path=?", (device_path,))
