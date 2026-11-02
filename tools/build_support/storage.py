"""SQLite ownership, atomic transactions and immutable event persistence."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .policy import baseline, digest, encoded, validate_policy
from .values import ControlError, _require

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
POLICY_TABLES = {
    "policy_revision",
    "amendment_receipt",
    "batch",
    "invocation_bound",
    "admission_state",
    "operation_receipt",
}
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

    @staticmethod
    def validate_backup(path: str, expected_sha: str, expected_state: str) -> None:
        target = Path(path)
        _require(
            target.is_file()
            and not target.is_symlink()
            and hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha,
            "backup_conflict",
            "The prepared backup changed or is unavailable.",
        )
        with sqlite3.connect(target.absolute().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            _require(
                LedgerTransaction(connection, target).state_digest() == expected_state,
                "backup_conflict",
                "The prepared backup has a different ledger state.",
            )

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
            yield LedgerTransaction(connection, self.path)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class LedgerTransaction:
    """Return copied records; all SQL and schema mechanics stay in this module."""

    def __init__(self, connection: sqlite3.Connection, path: Path):
        self._connection = connection
        self.path = path

    def schema_version(self) -> int:
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def ensure_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        names = {
            r[0]
            for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        _require(
            version in (0, LEGACY_SCHEMA_VERSION, SCHEMA_VERSION),
            "invalid_ledger",
            "Unsupported ledger schema.",
        )
        _require(version != 0 or not names, "invalid_ledger", "Refusing an unrelated database.")
        if version == SCHEMA_VERSION:
            self.run()
            return
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
            _require(
                version in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION),
                "invalid_ledger",
                "Unsupported ledger schema.",
            )
            row = self.find_run()
            if version == SCHEMA_VERSION:
                names = {
                    r[0]
                    for r in self._connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                _require(POLICY_TABLES <= names, "invalid_ledger", "Incomplete policy schema.")
                self.policy()
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
        self._connection.execute(f"PRAGMA user_version = {LEGACY_SCHEMA_VERSION}")

    def policy(self) -> dict[str, Any]:
        run = self.find_run()
        _require(run is not None, "invalid_ledger", "Missing build run.")
        if self.schema_version() == LEGACY_SCHEMA_VERSION:
            return baseline(run)
        _require(
            self.schema_version() == SCHEMA_VERSION, "invalid_ledger", "Unsupported policy schema."
        )
        rows = list(self._connection.execute("SELECT * FROM policy_revision ORDER BY revision"))
        _require(bool(rows), "invalid_ledger", "Missing effective policy.")
        parent = None
        for expected_revision, row in enumerate(rows):
            try:
                value = validate_policy(json.loads(row["data"]), row["policy_digest"])
            except (ValueError, KeyError, TypeError) as error:
                raise ControlError("invalid_ledger", "Invalid policy record.") from error
            _require(
                row["revision"] == expected_revision == value["revision"]
                and row["parent_digest"] == parent,
                "invalid_ledger",
                "Broken policy history.",
            )
            if expected_revision == 0:
                _require(value == baseline(run), "invalid_ledger", "Original policy changed.")
            parent = row["policy_digest"]
        return value

    def policy_digest(self) -> str:
        return digest(self.policy())

    def state_payload(self) -> dict[str, Any]:
        self.run()
        result = {
            "schema_version": self.schema_version(),
            "run": self.find_run(),
            "calls": self.calls(),
            "events": self.events(),
        }
        if self.schema_version() == SCHEMA_VERSION:
            for table in sorted(POLICY_TABLES):
                result[table] = [
                    dict(row)
                    for row in self._connection.execute(f"SELECT * FROM {table} ORDER BY 1")
                ]
        return result

    def state_digest(self) -> str:
        return digest(self.state_payload())

    def backup_to(self, destination: Path) -> dict[str, Any]:
        target = destination.absolute()
        forbidden = {
            Path(str(self.path) + suffix).resolve() for suffix in ("", "-wal", "-shm", "-journal")
        }
        _require(
            target.resolve() not in forbidden and not target.exists() and not target.is_symlink(),
            "invalid_input",
            "Backup needs a new separate path.",
        )
        _require(
            all(not parent.is_symlink() for parent in target.parents),
            "invalid_input",
            "Backup parents must not be symlinks.",
        )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            target, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        os.close(descriptor)
        # The surrounding BEGIN IMMEDIATE excludes other writers. A separate
        # read connection backs up that coherent committed before-image.
        with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(target) as destination_connection:
                source.backup(destination_connection)
                destination_connection.row_factory = sqlite3.Row
                backup = LedgerTransaction(destination_connection, target)
                _require(
                    backup.state_digest() == self.state_digest(),
                    "backup_conflict",
                    "Backup does not match the locked before-image.",
                )
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "path": str(target),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "ledger_digest": self.state_digest(),
        }

    def upgrade_policy_schema(self) -> None:
        if self.schema_version() == SCHEMA_VERSION:
            return
        _require(
            self.schema_version() == LEGACY_SCHEMA_VERSION,
            "invalid_ledger",
            "Only the known legacy schema can migrate.",
        )
        for statement in (
            "CREATE TABLE policy_revision(revision INTEGER PRIMARY KEY, "
            "policy_digest TEXT NOT NULL, "
            "parent_digest TEXT, data TEXT NOT NULL)",
            "CREATE TABLE amendment_receipt(operation_id TEXT PRIMARY KEY, "
            "request_digest TEXT NOT NULL, authorization_id TEXT NOT NULL UNIQUE, "
            "authorization_sha256 TEXT NOT NULL UNIQUE, receipt TEXT NOT NULL)",
            "CREATE TABLE batch(batch_id TEXT PRIMARY KEY, definition TEXT NOT NULL, "
            "review_sha256 TEXT NOT NULL, receipt TEXT NOT NULL)",
            "CREATE TABLE invocation_bound(call_id TEXT PRIMARY KEY REFERENCES call(call_id), "
            "batch_id TEXT NOT NULL REFERENCES batch(batch_id), timeout_seconds REAL NOT NULL, "
            "execution_deadline TEXT, policy_revision INTEGER NOT NULL, binding_sha256 TEXT)",
            "CREATE TABLE admission_state(singleton INTEGER PRIMARY KEY "
            "CHECK(singleton=1), paused INTEGER NOT NULL)",
            "CREATE TABLE operation_receipt(operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "request_digest TEXT NOT NULL, receipt TEXT NOT NULL)",
        ):
            self._connection.execute(statement)
        original = baseline(self.find_run())
        self.insert_policy(original, None)
        self._connection.execute("INSERT INTO admission_state VALUES(1,0)")
        self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def insert_policy(self, policy: dict[str, Any], parent_digest: str | None) -> None:
        self._connection.execute(
            "INSERT INTO policy_revision VALUES(?,?,?,?)",
            (policy["revision"], digest(policy), parent_digest, encoded(policy)),
        )

    def amendment(self, operation_id: str) -> dict[str, Any] | None:
        if self.schema_version() == LEGACY_SCHEMA_VERSION:
            return None
        row = self._connection.execute(
            "SELECT * FROM amendment_receipt WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def authorization_used(self, identity: str, sha: str) -> bool:
        if self.schema_version() == LEGACY_SCHEMA_VERSION:
            return False
        return (
            self._connection.execute(
                "SELECT 1 FROM amendment_receipt "
                "WHERE authorization_id=? OR authorization_sha256=?",
                (identity, sha),
            ).fetchone()
            is not None
        )

    def insert_amendment(
        self,
        operation_id: str,
        request_digest: str,
        authorization_id: str,
        authorization_sha: str,
        receipt: dict[str, Any],
    ) -> None:
        self._connection.execute(
            "INSERT INTO amendment_receipt VALUES(?,?,?,?,?)",
            (operation_id, request_digest, authorization_id, authorization_sha, encoded(receipt)),
        )

    def paused(self) -> bool:
        if self.schema_version() == LEGACY_SCHEMA_VERSION:
            return False
        row = self._connection.execute(
            "SELECT paused FROM admission_state WHERE singleton=1"
        ).fetchone()
        _require(row is not None and row[0] in (0, 1), "invalid_ledger", "Invalid admission state.")
        return bool(row[0])

    def set_paused(self, paused: bool) -> None:
        self._connection.execute(
            "UPDATE admission_state SET paused=? WHERE singleton=1", (int(paused),)
        )

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM operation_receipt WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def insert_operation(
        self, operation_id: str, kind: str, request_digest: str, receipt: dict[str, Any]
    ) -> None:
        self._connection.execute(
            "INSERT INTO operation_receipt VALUES(?,?,?,?)",
            (operation_id, kind, request_digest, encoded(receipt)),
        )

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM batch WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def batches(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute("SELECT * FROM batch ORDER BY rowid")]

    def insert_batch(
        self, definition: dict[str, Any], review_sha: str, receipt: dict[str, Any]
    ) -> None:
        self._connection.execute(
            "INSERT INTO batch VALUES(?,?,?,?)",
            (definition["batch_id"], encoded(definition), review_sha, encoded(receipt)),
        )

    def batch_totals(self, batch_id: str) -> tuple[int, int]:
        rows = list(
            self._connection.execute(
                "SELECT COALESCE(c.actual_micro,c.reserved_micro) FROM invocation_bound b "
                "JOIN call c USING(call_id) WHERE b.batch_id=?",
                (batch_id,),
            )
        )
        return len(rows), sum(row[0] for row in rows)

    def bound(self, call_id: str) -> dict[str, Any] | None:
        if self.schema_version() == LEGACY_SCHEMA_VERSION:
            return None
        row = self._connection.execute(
            "SELECT * FROM invocation_bound WHERE call_id=?", (call_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def insert_bound(
        self,
        call_id: str,
        batch_id: str,
        timeout: float,
        revision: int,
        binding_sha: str | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO invocation_bound VALUES(?,?,?,NULL,?,?)",
            (call_id, batch_id, timeout, revision, binding_sha),
        )

    def set_execution_deadline(self, call_id: str, deadline: str) -> None:
        self._connection.execute(
            "UPDATE invocation_bound SET execution_deadline=? "
            "WHERE call_id=? AND execution_deadline IS NULL",
            (deadline, call_id),
        )

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
        rows = self.calls()
        return (
            sum(row["actual_micro"] or 0 for row in rows),
            sum(row["reserved_micro"] for row in rows if row["actual_micro"] is None),
            sum(row["uses_worker"] and row["worker_state"] != "released" for row in rows),
        )

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
