"""SQLite ownership, atomic transactions and immutable event persistence."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .values import ControlError, _require

SCHEMA_VERSION = 1
CALL_INSERT_FIELDS = (
    "call_id",
    "label",
    "paid",
    "uses_worker",
    "reserved_micro",
    "status",
    "worker_state",
    "reserved_at",
    "metadata",
)
CALL_UPDATE_FIELDS = frozenset(
    {
        "status",
        "outcome",
        "worker_state",
        "started_at",
        "finished_at",
        "actual_micro",
        "worker_identity",
        "process_pid",
        "process_start_identity",
        "evidence",
        "reason",
        "process_stopped",
    }
)


class LedgerStore:
    """Own the ledger file and yield one transactional repository at a time."""

    def __init__(self, path: str | Path):
        self.path = Path(path).absolute()

    @contextmanager
    def transaction(self, *, create: bool = False) -> Iterator[LedgerTransaction]:
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
                )
                os.close(descriptor)
            except OSError as error:
                raise ControlError("invalid_ledger", "Cannot safely open the ledger.") from error
        _require(
            self.path.is_file() and not self.path.is_symlink(),
            "invalid_ledger",
            "Initialize a regular ledger file first.",
        )
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield LedgerTransaction(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class LedgerTransaction:
    """Return copied records; all SQL and schema mechanics stay in this module."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def ensure_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        names = {
            r[0]
            for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        _require(version in (0, SCHEMA_VERSION), "invalid_ledger", "Unsupported ledger schema.")
        _require(version != 0 or not names, "invalid_ledger", "Refusing an unrelated database.")
        for statement in (
            "CREATE TABLE IF NOT EXISTS run (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "configuration TEXT NOT NULL, run_id TEXT NOT NULL, started_at TEXT NOT NULL, "
            "deadline TEXT NOT NULL, budget_micro INTEGER NOT NULL, "
            "worker_cap INTEGER NOT NULL, admission_buffer_seconds INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS call (call_id TEXT PRIMARY KEY, label TEXT NOT NULL, "
            "paid INTEGER NOT NULL, uses_worker INTEGER NOT NULL, "
            "reserved_micro INTEGER NOT NULL, actual_micro INTEGER, "
            "status TEXT NOT NULL, outcome TEXT, worker_state TEXT NOT NULL, "
            "reserved_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "worker_identity TEXT, process_pid INTEGER, process_start_identity TEXT, "
            "metadata TEXT NOT NULL, evidence TEXT, reason TEXT, "
            "process_stopped INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
            "call_id TEXT, at_utc TEXT NOT NULL, payload TEXT NOT NULL)",
        ):
            self._connection.execute(statement)

    def find_run(self) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM run WHERE singleton = 1").fetchone()
        return None if row is None else dict(row)

    def run(self) -> dict[str, Any]:
        try:
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            _require(version == SCHEMA_VERSION, "invalid_ledger", "Unsupported ledger schema.")
            row = self.find_run()
        except sqlite3.DatabaseError as error:
            raise ControlError("invalid_ledger", "The ledger is unavailable or corrupt.") from error
        _require(row is not None, "invalid_ledger", "Initialize this build run first.")
        return row

    def insert_run(self, configuration: str, values: Mapping[str, Any]) -> None:
        self._connection.execute(
            "INSERT INTO run VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                configuration,
                values["run_id"],
                values["started_at"],
                values["deadline"],
                values["budget_micro"],
                values["initial_worker_cap"],
                values["admission_buffer_seconds"],
            ),
        )
        self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def append_event(
        self, kind: str, call_id: str | None, at_utc: str, payload: Mapping[str, Any]
    ) -> None:
        self._connection.execute(
            "INSERT INTO event(kind, call_id, at_utc, payload) VALUES (?, ?, ?, ?)",
            (kind, call_id, at_utc, json.dumps(payload, sort_keys=True, allow_nan=False)),
        )

    def find_call(self, call_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM call WHERE call_id = ?", (call_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def call(self, call_id: str) -> dict[str, Any]:
        row = self.find_call(call_id)
        _require(row is not None, "unknown_call", "Call reservation does not exist.")
        return row

    def insert_call(self, values: Mapping[str, Any]) -> None:
        _require(
            set(values) == set(CALL_INSERT_FIELDS),
            "invalid_input",
            "Call persistence requires its declared fields.",
        )
        columns = ",".join(CALL_INSERT_FIELDS)
        placeholders = ",".join("?" for _ in CALL_INSERT_FIELDS)
        self._connection.execute(
            f"INSERT INTO call ({columns}) VALUES ({placeholders})",
            tuple(values[name] for name in CALL_INSERT_FIELDS),
        )

    def update_call(self, call_id: str, **changes: Any) -> None:
        _require(
            bool(changes) and set(changes) <= CALL_UPDATE_FIELDS,
            "invalid_input",
            "Only declared mutable call fields can be updated.",
        )
        columns = ",".join(name + "=?" for name in changes)
        self._connection.execute(
            f"UPDATE call SET {columns} WHERE call_id=?", (*changes.values(), call_id)
        )

    def totals(self) -> tuple[int, int, int]:
        row = self._connection.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_micro,0)),0) AS spent, "
            "COALESCE(SUM(CASE WHEN actual_micro IS NULL "
            "THEN reserved_micro ELSE 0 END),0) AS held, "
            "COALESCE(SUM(CASE WHEN uses_worker=1 AND worker_state!='released' "
            "THEN 1 ELSE 0 END),0) AS workers FROM call"
        ).fetchone()
        return row["spent"], row["held"], row["workers"]

    def has_event_kind(self, kind: str) -> bool:
        return (
            self._connection.execute("SELECT 1 FROM event WHERE kind=? LIMIT 1", (kind,)).fetchone()
            is not None
        )

    def update_worker_cap(self, cap: int) -> None:
        self._connection.execute("UPDATE run SET worker_cap=? WHERE singleton=1", (cap,))

    def calls(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute("SELECT * FROM call ORDER BY rowid")]

    def events(self) -> list[dict[str, Any]]:
        result = [dict(row) for row in self._connection.execute("SELECT * FROM event ORDER BY id")]
        for event in result:
            event["payload"] = json.loads(event["payload"])
        return result
