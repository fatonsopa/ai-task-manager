#!/usr/bin/env python3
"""Durable limits for this build, separate from the product task-state engine.

Money is stored in integer millionths of a US dollar. A reservation is held
until an actual stopped invocation is reconciled, including after a crash.
This module neither authenticates model providers nor infers their charges.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MAX_SECONDS = 48 * 60 * 60
MAX_BUDGET_MICRO = 100_000_000
SCHEMA_VERSION = 1
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ACTIVE_WORKER_STATES = ("reserved", "running", "uncertain")


class ControlError(Exception):
    """A refused operation, with a stable category for callers."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ControlError(code, message)


def _text(value: str, name: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()), "invalid_input", f"{name} is required."
    )
    return value


def _identifier(value: str, name: str) -> str:
    _require(
        isinstance(value, str) and bool(IDENTIFIER.fullmatch(value)),
        "invalid_input",
        f"{name} must be a bounded identifier.",
    )
    return value


def _utc(value: datetime | str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise ControlError(
            "invalid_input", "Use an ISO timestamp with an explicit timezone."
        ) from error
    _require(
        isinstance(parsed, datetime)
        and parsed.tzinfo is not None
        and parsed.utcoffset() is not None,
        "invalid_input",
        "Timestamps need a timezone.",
    )
    return parsed.astimezone(UTC)


def _money(value: str | int | float | Decimal) -> int:
    _require(
        not isinstance(value, bool), "invalid_input", "Money must be a finite nonnegative amount."
    )
    try:
        amount = Decimal(str(value))
        scaled = amount * 1_000_000
        valid = amount.is_finite() and amount >= 0 and scaled == scaled.to_integral_value()
    except (InvalidOperation, ValueError) as error:
        raise ControlError("invalid_input", "Money needs at most six decimal places.") from error
    _require(
        valid and scaled <= 9_223_372_036_854_775_807,
        "invalid_input",
        "Money must be finite, nonnegative, and exact to six places within storage limits.",
    )
    return int(scaled)


def _usd(micro: int) -> str:
    return f"{Decimal(micro) / 1_000_000:.6f}"


class BuildControl:
    """One build run per SQLite file; opening another run cannot reset its limits."""

    def __init__(self, ledger: str | Path, *, clock: Callable[[], datetime] | None = None):
        self.ledger = Path(ledger).absolute()
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> str:
        return _utc(self.clock()).isoformat()

    @contextmanager
    def _connection(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        if create:
            self.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(
                    self.ledger, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
                )
                os.close(descriptor)
            except OSError as error:
                raise ControlError("invalid_ledger", "Cannot safely open the ledger.") from error
        _require(
            self.ledger.is_file() and not self.ledger.is_symlink(),
            "invalid_ledger",
            "Initialize a regular ledger file first.",
        )
        connection = sqlite3.connect(self.ledger, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _run(connection: sqlite3.Connection) -> sqlite3.Row:
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            _require(version == SCHEMA_VERSION, "invalid_ledger", "Unsupported ledger schema.")
            row = connection.execute("SELECT * FROM run WHERE singleton = 1").fetchone()
        except sqlite3.DatabaseError as error:
            raise ControlError("invalid_ledger", "The ledger is unavailable or corrupt.") from error
        _require(row is not None, "invalid_ledger", "Initialize this build run first.")
        return row

    def _event(
        self,
        connection: sqlite3.Connection,
        kind: str,
        call_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO event(kind, call_id, at_utc, payload) VALUES (?, ?, ?, ?)",
            (kind, call_id, self._now(), json.dumps(payload, sort_keys=True, allow_nan=False)),
        )

    def initialize(
        self,
        *,
        run_id: str,
        started_at: datetime | str,
        budget_usd: str | int | Decimal = "100",
        worker_cap: int = 2,
        duration_seconds: int = MAX_SECONDS,
        admission_buffer_seconds: int = 1800,
    ) -> dict[str, Any]:
        _identifier(run_id, "run_id")
        start = _utc(started_at)
        budget = _money(budget_usd)
        _require(0 < budget <= MAX_BUDGET_MICRO, "invalid_input", "Budget must be within USD 100.")
        self._valid_cap(worker_cap)
        _require(
            type(duration_seconds) is int and 0 < duration_seconds <= MAX_SECONDS,
            "invalid_input",
            "Duration must be a positive number of seconds within 48 hours.",
        )
        _require(
            type(admission_buffer_seconds) is int
            and 0 <= admission_buffer_seconds < duration_seconds,
            "invalid_input",
            "Admission buffer must fit within the run duration.",
        )
        configuration = json.dumps(
            {
                "run_id": run_id,
                "started_at": start.isoformat(),
                "deadline": (start + timedelta(seconds=duration_seconds)).isoformat(),
                "budget_micro": budget,
                "initial_worker_cap": worker_cap,
                "admission_buffer_seconds": admission_buffer_seconds,
            },
            sort_keys=True,
        )
        with self._connection(create=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            names = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            _require(version in (0, SCHEMA_VERSION), "invalid_ledger", "Unsupported ledger schema.")
            _require(version != 0 or not names, "invalid_ledger", "Refusing an unrelated database.")
            for statement in (
                "CREATE TABLE IF NOT EXISTS run (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "configuration TEXT NOT NULL, run_id TEXT NOT NULL, started_at TEXT NOT NULL, "
                "deadline TEXT NOT NULL, budget_micro INTEGER NOT NULL, "
                "worker_cap INTEGER NOT NULL, "
                "admission_buffer_seconds INTEGER NOT NULL)",
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
                connection.execute(statement)
            existing = connection.execute("SELECT * FROM run WHERE singleton = 1").fetchone()
            if existing:
                _require(
                    existing["configuration"] == configuration,
                    "run_conflict",
                    "Existing start, deadline and initial limits cannot be reset.",
                )
            else:
                config = json.loads(configuration)
                connection.execute(
                    "INSERT INTO run VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        configuration,
                        run_id,
                        config["started_at"],
                        config["deadline"],
                        budget,
                        worker_cap,
                        admission_buffer_seconds,
                    ),
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._event(connection, "run_initialized", None, config)
        return self.snapshot()

    @staticmethod
    def _valid_cap(cap: int) -> None:
        _require(
            type(cap) is int and 0 < cap <= 1_000_000,
            "invalid_input",
            "Worker cap must be a positive finite integer.",
        )

    @staticmethod
    def _totals(connection: sqlite3.Connection) -> tuple[int, int, int]:
        row = connection.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_micro,0)),0) AS spent, "
            "COALESCE(SUM(CASE WHEN actual_micro IS NULL "
            "THEN reserved_micro ELSE 0 END),0) AS held, "
            "COALESCE(SUM(CASE WHEN uses_worker=1 AND worker_state!='released' "
            "THEN 1 ELSE 0 END),0) AS workers "
            "FROM call"
        ).fetchone()
        return row["spent"], row["held"], row["workers"]

    @staticmethod
    def _call(connection: sqlite3.Connection, call_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM call WHERE call_id = ?", (call_id,)).fetchone()
        _require(row is not None, "unknown_call", "Call reservation does not exist.")
        return row

    @staticmethod
    def _public_call(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["reserved_usd"] = _usd(result.pop("reserved_micro"))
        actual = result.pop("actual_micro")
        result["actual_usd"] = None if actual is None else _usd(actual)
        result["cost_settled"] = actual is not None
        result["metadata"] = json.loads(result["metadata"])
        result["paid"] = bool(result["paid"])
        result["uses_worker"] = bool(result["uses_worker"])
        result["process_stopped"] = bool(result["process_stopped"])
        return result

    def reserve(
        self,
        call_id: str,
        *,
        label: str,
        paid: bool,
        max_cost_usd: str | int | Decimal | None,
        uses_worker: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _identifier(call_id, "call_id")
        _text(label, "label")
        _require(
            type(paid) is bool and type(uses_worker) is bool,
            "invalid_input",
            "paid and uses_worker must be booleans.",
        )
        _require(
            not paid or max_cost_usd is not None,
            "unknown_cost",
            "A paid call needs an enforceable maximum charge before admission.",
        )
        maximum = _money(max_cost_usd if max_cost_usd is not None else 0)
        _require(
            (paid and maximum > 0) or (not paid and maximum == 0),
            "invalid_input",
            "Paid calls need a positive bound; unpaid calls must have a zero bound.",
        )
        _require(
            metadata is None or isinstance(metadata, dict),
            "invalid_input",
            "Metadata must be a JSON object.",
        )
        try:
            encoded = json.dumps(metadata or {}, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ControlError("invalid_input", "Metadata must be finite JSON data.") from error
        with self._connection() as connection:
            run = self._run(connection)
            existing = connection.execute(
                "SELECT * FROM call WHERE call_id=?", (call_id,)
            ).fetchone()
            if existing:
                _require(
                    (
                        existing["label"],
                        existing["paid"],
                        existing["uses_worker"],
                        existing["reserved_micro"],
                        existing["metadata"],
                    )
                    == (label, int(paid), int(uses_worker), maximum, encoded),
                    "call_conflict",
                    "Reservation identifier already has different inputs.",
                )
                return self._public_call(existing)
            now = _utc(self.clock())
            cutoff = _utc(run["deadline"]) - timedelta(seconds=run["admission_buffer_seconds"])
            _require(now >= _utc(run["started_at"]), "clock_conflict", "Clock predates this run.")
            _require(now < cutoff, "deadline_reached", "New admission has stopped for this run.")
            spent, held, workers = self._totals(connection)
            _require(
                not connection.execute(
                    "SELECT 1 FROM event WHERE kind='charge_bound_exceeded' LIMIT 1"
                ).fetchone(),
                "budget_integrity_failure",
                "A charge exceeded its bound; new admission requires operator reconciliation.",
            )
            _require(
                spent + held + maximum <= run["budget_micro"],
                "budget_exceeded",
                "Insufficient unreserved build budget.",
            )
            _require(
                not uses_worker or workers < run["worker_cap"],
                "capacity_exceeded",
                "All build worker slots are reserved.",
            )
            connection.execute(
                "INSERT INTO call (call_id,label,paid,uses_worker,reserved_micro,"
                "status,worker_state,"
                "reserved_at,metadata) VALUES (?,?,?,?,?,'reserved',?,?,?)",
                (
                    call_id,
                    label,
                    int(paid),
                    int(uses_worker),
                    maximum,
                    "reserved" if uses_worker else "released",
                    now.isoformat(),
                    encoded,
                ),
            )
            self._event(
                connection,
                "call_reserved",
                call_id,
                {"maximum_usd": _usd(maximum), "uses_worker": uses_worker},
            )
            result = self._public_call(self._call(connection, call_id))
        return result

    def claim_launch(self, call_id: str, *, worker_identity: str) -> dict[str, Any]:
        """Claim the external launch before spawning; a lost claim stays reserved."""
        _text(worker_identity, "worker_identity")
        with self._connection() as connection:
            run = self._run(connection)
            call = self._call(connection, call_id)
            _require(
                call["status"] == "reserved",
                "already_executed",
                "Another caller already claimed or executed this invocation.",
            )
            _require(
                _utc(self.clock()) < _utc(run["deadline"]),
                "deadline_reached",
                "The hard deadline has passed; do not launch a reserved invocation.",
            )
            connection.execute(
                "UPDATE call SET status='launching',worker_identity=? WHERE call_id=?",
                (worker_identity, call_id),
            )
            self._event(connection, "launch_claimed", call_id, {"worker_identity": worker_identity})
            return self._public_call(self._call(connection, call_id))

    def mark_started(
        self,
        call_id: str,
        *,
        worker_identity: str,
        process_pid: int | None = None,
        process_start_identity: str | None = None,
    ) -> dict[str, Any]:
        _text(worker_identity, "worker_identity")
        if process_pid is not None:
            _require(type(process_pid) is int and process_pid > 0, "invalid_input", "Invalid PID.")
            _text(process_start_identity, "process_start_identity")
        with self._connection() as connection:
            self._run(connection)
            call = self._call(connection, call_id)
            if call["status"] == "running":
                _require(
                    (call["worker_identity"], call["process_pid"], call["process_start_identity"])
                    == (worker_identity, process_pid, process_start_identity),
                    "call_conflict",
                    "Another invocation already owns this reservation.",
                )
                return self._public_call(call)
            _require(
                call["status"] in {"reserved", "launching"},
                "invalid_transition",
                "This call cannot start.",
            )
            _require(
                call["status"] != "launching" or call["worker_identity"] == worker_identity,
                "call_conflict",
                "A different launch owns this reservation.",
            )
            connection.execute(
                "UPDATE call SET status='running',started_at=?,worker_identity=?,process_pid=?,"
                "process_start_identity=?,worker_state=? WHERE call_id=?",
                (
                    self._now(),
                    worker_identity,
                    process_pid,
                    process_start_identity,
                    "running" if call["uses_worker"] else "released",
                    call_id,
                ),
            )
            self._event(connection, "call_started", call_id, {"worker_identity": worker_identity})
            return self._public_call(self._call(connection, call_id))

    def record_outcome(
        self,
        call_id: str,
        *,
        outcome: str,
        process_stopped: bool,
        evidence_ref: str,
        actual_cost_usd: str | int | Decimal | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        _require(
            outcome in {"succeeded", "failed", "interrupted", "unknown"},
            "invalid_input",
            "Unsupported invocation outcome.",
        )
        _require(
            type(process_stopped) is bool, "invalid_input", "Confirm process_stopped explicitly."
        )
        _text(evidence_ref, "evidence_ref")
        _require(
            actual_cost_usd is None or process_stopped,
            "unsettled_process",
            "Cost cannot be finalized while the process may still execute.",
        )
        actual = None if actual_cost_usd is None else _money(actual_cost_usd)
        with self._connection() as connection:
            self._run(connection)
            call = self._call(connection, call_id)
            _require(
                not call["process_stopped"] or process_stopped,
                "call_conflict",
                "A confirmed stop cannot be replaced by an active process claim.",
            )
            if call["status"] in {"succeeded", "failed"}:
                _require(
                    call["outcome"] == outcome,
                    "call_conflict",
                    "A final outcome cannot be replaced.",
                )
            _require(
                outcome != "succeeded" or call["status"] in {"running", "unknown", "succeeded"},
                "invalid_transition",
                "An unstarted reservation cannot succeed.",
            )
            _require(
                call["status"] != "cancelled",
                "invalid_transition",
                "A cancelled call cannot execute.",
            )
            if call["actual_micro"] is not None:
                _require(
                    actual is None or actual == call["actual_micro"],
                    "cost_conflict",
                    "A settled charge cannot be overwritten.",
                )
                actual = call["actual_micro"]
            if not call["paid"] and process_stopped:
                _require(
                    actual in (None, 0), "invalid_input", "An unpaid call cannot report a charge."
                )
                actual = 0
            worker_state = "released" if process_stopped or not call["uses_worker"] else "uncertain"
            status = "failed" if outcome == "interrupted" else outcome
            reason = reason if reason is not None else call["reason"]
            connection.execute(
                "UPDATE call SET status=?,outcome=?,worker_state=?,finished_at=?,actual_micro=?,"
                "evidence=?,reason=?,process_stopped=? WHERE call_id=?",
                (
                    status,
                    outcome,
                    worker_state,
                    self._now() if process_stopped else call["finished_at"],
                    actual,
                    evidence_ref,
                    reason,
                    int(process_stopped),
                    call_id,
                ),
            )
            self._event(
                connection,
                "call_outcome",
                call_id,
                {
                    "status": status,
                    "outcome": outcome,
                    "process_stopped": process_stopped,
                    "actual_usd": None if actual is None else _usd(actual),
                    "reason": reason,
                    "evidence": evidence_ref,
                },
            )
            if actual is not None and actual > call["reserved_micro"]:
                self._event(
                    connection,
                    "charge_bound_exceeded",
                    call_id,
                    {"reserved_usd": _usd(call["reserved_micro"]), "actual_usd": _usd(actual)},
                )
            return self._public_call(self._call(connection, call_id))

    def settle_cost(
        self, call_id: str, *, actual_cost_usd: str | int | Decimal, evidence_ref: str
    ) -> dict[str, Any]:
        _text(evidence_ref, "evidence_ref")
        actual = _money(actual_cost_usd)
        with self._connection() as connection:
            self._run(connection)
            call = self._call(connection, call_id)
            _require(
                call["status"] not in {"reserved", "launching", "running"}
                and call["process_stopped"],
                "unsettled_process",
                "Confirm the invocation stopped before finalizing its charge.",
            )
            _require(
                call["actual_micro"] is None or call["actual_micro"] == actual,
                "cost_conflict",
                "A settled charge cannot be overwritten.",
            )
            _require(
                call["paid"] or actual == 0,
                "invalid_input",
                "An unpaid call cannot report a charge.",
            )
            connection.execute("UPDATE call SET actual_micro=? WHERE call_id=?", (actual, call_id))
            self._event(
                connection,
                "cost_settled",
                call_id,
                {"actual_usd": _usd(actual), "evidence": evidence_ref},
            )
            if actual > call["reserved_micro"]:
                self._event(
                    connection,
                    "charge_bound_exceeded",
                    call_id,
                    {"reserved_usd": _usd(call["reserved_micro"]), "actual_usd": _usd(actual)},
                )
            return self._public_call(self._call(connection, call_id))

    def cancel_unstarted(
        self, call_id: str, *, confirmed_not_started: bool, evidence_ref: str
    ) -> dict[str, Any]:
        _require(
            confirmed_not_started is True,
            "unconfirmed_start",
            "Reconcile an uncertain launch instead of releasing its reservation.",
        )
        _text(evidence_ref, "evidence_ref")
        with self._connection() as connection:
            self._run(connection)
            call = self._call(connection, call_id)
            _require(
                call["status"] in {"reserved", "cancelled"},
                "invalid_transition",
                "Only a confirmed unstarted reservation can be cancelled.",
            )
            connection.execute(
                "UPDATE call SET status='cancelled',outcome='not_started',"
                "worker_state='released',process_stopped=1,actual_micro=0,finished_at=?,evidence=? "
                "WHERE call_id=?",
                (self._now(), evidence_ref, call_id),
            )
            self._event(connection, "unstarted_call_cancelled", call_id, {"evidence": evidence_ref})
            return self._public_call(self._call(connection, call_id))

    def set_worker_cap(self, worker_cap: int, *, evidence_ref: str) -> dict[str, Any]:
        self._valid_cap(worker_cap)
        _text(evidence_ref, "evidence_ref")
        with self._connection() as connection:
            self._run(connection)
            _require(
                self._totals(connection)[2] <= worker_cap,
                "capacity_conflict",
                "Cannot lower capacity below existing reservations.",
            )
            connection.execute("UPDATE run SET worker_cap=? WHERE singleton=1", (worker_cap,))
            self._event(
                connection,
                "worker_cap_changed",
                None,
                {"worker_cap": worker_cap, "evidence": evidence_ref},
            )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._connection() as connection:
            run = self._run(connection)
            spent, held, workers = self._totals(connection)
            events = [dict(row) for row in connection.execute("SELECT * FROM event ORDER BY id")]
            for event in events:
                event["payload"] = json.loads(event["payload"])
            calls = [
                self._public_call(row)
                for row in connection.execute("SELECT * FROM call ORDER BY rowid")
            ]
            cutoff = _utc(run["deadline"]) - timedelta(seconds=run["admission_buffer_seconds"])
            return {
                "schema_version": SCHEMA_VERSION,
                "run_id": run["run_id"],
                "started_at": run["started_at"],
                "deadline": run["deadline"],
                "admission_stops_at": cutoff.isoformat(),
                "observed_at": self._now(),
                "worker_cap": run["worker_cap"],
                "active_workers": workers,
                "budget_usd": _usd(run["budget_micro"]),
                "settled_usd": _usd(spent),
                "reserved_usd": _usd(held),
                "available_usd": _usd(run["budget_micro"] - spent - held),
                "budget_breached": spent + held > run["budget_micro"],
                "calls": calls,
                "events": events,
            }

    def checkpoint(self, destination: str | Path) -> dict[str, Any]:
        target = Path(destination).absolute()
        forbidden = {
            Path(str(self.ledger) + suffix).resolve() for suffix in ("", "-wal", "-shm", "-journal")
        }
        _require(
            target.resolve() not in forbidden and not target.is_symlink(),
            "invalid_input",
            "Checkpoint must be a separate regular path.",
        )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (json.dumps(self.snapshot(), indent=2, sort_keys=True) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"path": str(target), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def run_command(
    control: BuildControl,
    *,
    call_id: str,
    command: list[str],
    paid: bool,
    max_cost_usd: str | None,
    timeout_seconds: float,
    output_directory: str | Path,
) -> dict[str, Any]:
    """Run one bounded argv without a shell; charges stay reserved until reconciled."""
    _require(
        isinstance(command, list)
        and bool(command)
        and all(isinstance(arg, str) and arg for arg in command),
        "invalid_input",
        "A command argument list is required.",
    )
    _require(
        isinstance(timeout_seconds, (int, float))
        and not isinstance(timeout_seconds, bool)
        and 0 < timeout_seconds < float("inf"),
        "invalid_input",
        "Timeout must be positive and finite.",
    )
    reserved = control.reserve(
        call_id, label="bounded command", paid=paid, max_cost_usd=max_cost_usd
    )
    _require(
        reserved["status"] == "reserved",
        "already_executed",
        "This invocation has already started or finished; reconcile instead of relaunching.",
    )
    invocation = str(uuid.uuid4())
    control.claim_launch(call_id, worker_identity=invocation)
    output = Path(output_directory).absolute()
    stdout_path, stderr_path = output / f"{call_id}.stdout", output / f"{call_id}.stderr"
    process: subprocess.Popen[bytes] | None = None
    try:
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, start_new_session=True
            )
            identity = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            control.mark_started(
                call_id,
                worker_identity=invocation,
                process_pid=process.pid if identity else None,
                process_start_identity=f"ps:{identity}" if identity else None,
            )
            remaining = (
                _utc(control.snapshot()["deadline"]) - _utc(control.clock())
            ).total_seconds()
            try:
                code = process.wait(timeout=max(0.001, min(timeout_seconds, remaining)))
                outcome, reason = ("succeeded", None) if code == 0 else ("failed", f"exit:{code}")
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                # The command's parent can exit before other members of its group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                code, outcome, reason = process.returncode, "interrupted", "timeout"
            result = control.record_outcome(
                call_id,
                outcome=outcome,
                process_stopped=True,
                evidence_ref=str(stdout_path),
                reason=reason,
            )
            return {
                "call": result,
                "returncode": code,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            }
    except FileExistsError as error:
        # A competing launch or a prior partial invocation may own these traces.
        raise ControlError(
            "launch_uncertain", "Existing invocation traces require reconciliation."
        ) from error
    except BaseException as error:
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        try:
            control.record_outcome(
                call_id,
                outcome="failed",
                process_stopped=True,
                evidence_ref=str(stderr_path),
                reason=type(error).__name__,
                actual_cost_usd="0" if process is None else None,
            )
        except (ControlError, sqlite3.Error):
            pass  # The existing durable reservation remains held if storage itself failed.
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("init")
    create.add_argument("--run-id", required=True)
    create.add_argument("--started-at", required=True)
    create.add_argument("--budget-usd", default="100")
    create.add_argument("--worker-cap", type=int, default=2)
    create.add_argument("--duration-seconds", type=int, default=MAX_SECONDS)
    create.add_argument("--admission-buffer-seconds", type=int, default=1800)
    commands.add_parser("status")
    reserve = commands.add_parser("reserve")
    reserve.add_argument("call_id")
    reserve.add_argument("--label", required=True)
    reserve.add_argument("--paid", action="store_true")
    reserve.add_argument("--max-cost-usd")
    reserve.add_argument("--without-worker", action="store_true")
    start = commands.add_parser("start")
    start.add_argument("call_id")
    start.add_argument("--worker-identity", required=True)
    finish = commands.add_parser("finish")
    finish.add_argument("call_id")
    finish.add_argument(
        "--outcome", choices=["succeeded", "failed", "interrupted", "unknown"], required=True
    )
    finish.add_argument("--process-stopped", action="store_true")
    finish.add_argument("--actual-cost-usd")
    finish.add_argument("--evidence-ref", required=True)
    finish.add_argument("--reason")
    settle = commands.add_parser("settle")
    settle.add_argument("call_id")
    settle.add_argument("--actual-cost-usd", required=True)
    settle.add_argument("--evidence-ref", required=True)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("destination", type=Path)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("call_id")
    cancel.add_argument("--confirm-not-started", action="store_true", required=True)
    cancel.add_argument("--evidence-ref", required=True)
    cap = commands.add_parser("worker-cap")
    cap.add_argument("value", type=int)
    cap.add_argument("--evidence-ref", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("call_id")
    execute.add_argument("--paid", action="store_true")
    execute.add_argument("--max-cost-usd")
    execute.add_argument("--timeout-seconds", type=float, required=True)
    execute.add_argument("--output-directory", type=Path, required=True)
    execute.add_argument(
        "--command-json",
        type=json.loads,
        required=True,
        help="Command argv as a JSON array; no shell is used.",
    )
    arguments = parser.parse_args(argv)
    control = BuildControl(arguments.ledger)
    try:
        match arguments.action:
            case "init":
                result = control.initialize(
                    run_id=arguments.run_id,
                    started_at=arguments.started_at,
                    budget_usd=arguments.budget_usd,
                    worker_cap=arguments.worker_cap,
                    duration_seconds=arguments.duration_seconds,
                    admission_buffer_seconds=arguments.admission_buffer_seconds,
                )
            case "status":
                result = control.snapshot()
            case "reserve":
                result = control.reserve(
                    arguments.call_id,
                    label=arguments.label,
                    paid=arguments.paid,
                    max_cost_usd=arguments.max_cost_usd,
                    uses_worker=not arguments.without_worker,
                )
            case "start":
                result = control.mark_started(
                    arguments.call_id, worker_identity=arguments.worker_identity
                )
            case "finish":
                result = control.record_outcome(
                    arguments.call_id,
                    outcome=arguments.outcome,
                    process_stopped=arguments.process_stopped,
                    actual_cost_usd=arguments.actual_cost_usd,
                    evidence_ref=arguments.evidence_ref,
                    reason=arguments.reason,
                )
            case "settle":
                result = control.settle_cost(
                    arguments.call_id,
                    actual_cost_usd=arguments.actual_cost_usd,
                    evidence_ref=arguments.evidence_ref,
                )
            case "cancel":
                result = control.cancel_unstarted(
                    arguments.call_id,
                    confirmed_not_started=arguments.confirm_not_started,
                    evidence_ref=arguments.evidence_ref,
                )
            case "worker-cap":
                result = control.set_worker_cap(
                    arguments.value, evidence_ref=arguments.evidence_ref
                )
            case "checkpoint":
                result = control.checkpoint(arguments.destination)
            case "run":
                result = run_command(
                    control,
                    call_id=arguments.call_id,
                    command=arguments.command_json,
                    paid=arguments.paid,
                    max_cost_usd=arguments.max_cost_usd,
                    timeout_seconds=arguments.timeout_seconds,
                    output_directory=arguments.output_directory,
                )
        print(json.dumps(result, indent=2))
        return 0
    except (ControlError, sqlite3.Error, OSError) as error:
        code = error.code if isinstance(error, ControlError) else "storage_or_process_error"
        print(json.dumps({"error": {"code": code, "message": str(error)}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
