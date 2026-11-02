"""Build admission, invocation outcomes and accounting decisions."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .checkpoint import publish_checkpoint
from .storage import SCHEMA_VERSION, LedgerStore, LedgerTransaction
from .values import (
    MAX_BUDGET_MICRO,
    MAX_SECONDS,
    ControlError,
    _identifier,
    _money,
    _require,
    _text,
    _usd,
    _utc,
    validate_worker_cap,
)


class BuildControl:
    """One build run per SQLite file; opening another run cannot reset its limits."""

    def __init__(self, ledger: str | Path, *, clock: Callable[[], datetime] | None = None):
        self._store = LedgerStore(ledger)
        self.ledger = self._store.path
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> str:
        return _utc(self.clock()).isoformat()

    def _event(
        self,
        transaction: LedgerTransaction,
        kind: str,
        call_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        transaction.append_event(kind, call_id, self._now(), payload)

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
        with self._store.transaction(create=True) as transaction:
            transaction.ensure_schema()
            existing = transaction.find_run()
            if existing:
                _require(
                    existing["configuration"] == configuration,
                    "run_conflict",
                    "Existing start, deadline and initial limits cannot be reset.",
                )
            else:
                config = json.loads(configuration)
                transaction.insert_run(configuration, config)
                self._event(transaction, "run_initialized", None, config)
        return self.snapshot()

    @staticmethod
    def _valid_cap(cap: int) -> None:
        validate_worker_cap(cap)

    @staticmethod
    def _public_call(row: dict[str, Any]) -> dict[str, Any]:
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
            paid and maximum > 0 or (not paid and maximum == 0),
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
        with self._store.transaction() as transaction:
            run = transaction.run()
            existing = transaction.find_call(call_id)
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
            spent, held, workers = transaction.totals()
            _require(
                not transaction.has_event_kind("charge_bound_exceeded"),
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
            transaction.insert_call(
                {
                    "call_id": call_id,
                    "label": label,
                    "paid": int(paid),
                    "uses_worker": int(uses_worker),
                    "reserved_micro": maximum,
                    "status": "reserved",
                    "worker_state": "reserved" if uses_worker else "released",
                    "reserved_at": now.isoformat(),
                    "metadata": encoded,
                }
            )
            self._event(
                transaction,
                "call_reserved",
                call_id,
                {"maximum_usd": _usd(maximum), "uses_worker": uses_worker},
            )
            result = self._public_call(transaction.call(call_id))
        return result

    def claim_launch(self, call_id: str, *, worker_identity: str) -> dict[str, Any]:
        """Claim the external launch before spawning; a lost claim stays reserved."""
        _text(worker_identity, "worker_identity")
        with self._store.transaction() as transaction:
            run = transaction.run()
            call = transaction.call(call_id)
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
            transaction.update_call(call_id, status="launching", worker_identity=worker_identity)
            self._event(
                transaction, "launch_claimed", call_id, {"worker_identity": worker_identity}
            )
            return self._public_call(transaction.call(call_id))

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
        with self._store.transaction() as transaction:
            transaction.run()
            call = transaction.call(call_id)
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
            transaction.update_call(
                call_id,
                status="running",
                started_at=self._now(),
                worker_identity=worker_identity,
                process_pid=process_pid,
                process_start_identity=process_start_identity,
                worker_state="running" if call["uses_worker"] else "released",
            )
            self._event(transaction, "call_started", call_id, {"worker_identity": worker_identity})
            return self._public_call(transaction.call(call_id))

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
        with self._store.transaction() as transaction:
            transaction.run()
            call = transaction.call(call_id)
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
            transaction.update_call(
                call_id,
                status=status,
                outcome=outcome,
                worker_state=worker_state,
                finished_at=self._now() if process_stopped else call["finished_at"],
                actual_micro=actual,
                evidence=evidence_ref,
                reason=reason,
                process_stopped=int(process_stopped),
            )
            self._event(
                transaction,
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
                    transaction,
                    "charge_bound_exceeded",
                    call_id,
                    {"reserved_usd": _usd(call["reserved_micro"]), "actual_usd": _usd(actual)},
                )
            return self._public_call(transaction.call(call_id))

    def settle_cost(
        self, call_id: str, *, actual_cost_usd: str | int | Decimal, evidence_ref: str
    ) -> dict[str, Any]:
        _text(evidence_ref, "evidence_ref")
        actual = _money(actual_cost_usd)
        with self._store.transaction() as transaction:
            transaction.run()
            call = transaction.call(call_id)
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
            transaction.update_call(call_id, actual_micro=actual)
            self._event(
                transaction,
                "cost_settled",
                call_id,
                {"actual_usd": _usd(actual), "evidence": evidence_ref},
            )
            if actual > call["reserved_micro"]:
                self._event(
                    transaction,
                    "charge_bound_exceeded",
                    call_id,
                    {"reserved_usd": _usd(call["reserved_micro"]), "actual_usd": _usd(actual)},
                )
            return self._public_call(transaction.call(call_id))

    def cancel_unstarted(
        self, call_id: str, *, confirmed_not_started: bool, evidence_ref: str
    ) -> dict[str, Any]:
        _require(
            confirmed_not_started is True,
            "unconfirmed_start",
            "Reconcile an uncertain launch instead of releasing its reservation.",
        )
        _text(evidence_ref, "evidence_ref")
        with self._store.transaction() as transaction:
            transaction.run()
            call = transaction.call(call_id)
            _require(
                call["status"] in {"reserved", "cancelled"},
                "invalid_transition",
                "Only a confirmed unstarted reservation can be cancelled.",
            )
            transaction.update_call(
                call_id,
                status="cancelled",
                outcome="not_started",
                worker_state="released",
                process_stopped=1,
                actual_micro=0,
                finished_at=self._now(),
                evidence=evidence_ref,
            )
            self._event(
                transaction, "unstarted_call_cancelled", call_id, {"evidence": evidence_ref}
            )
            return self._public_call(transaction.call(call_id))

    def set_worker_cap(self, worker_cap: int, *, evidence_ref: str) -> dict[str, Any]:
        self._valid_cap(worker_cap)
        _text(evidence_ref, "evidence_ref")
        with self._store.transaction() as transaction:
            transaction.run()
            _require(
                transaction.totals()[2] <= worker_cap,
                "capacity_conflict",
                "Cannot lower capacity below existing reservations.",
            )
            transaction.update_worker_cap(worker_cap)
            self._event(
                transaction,
                "worker_cap_changed",
                None,
                {"worker_cap": worker_cap, "evidence": evidence_ref},
            )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._store.transaction() as transaction:
            run = transaction.run()
            spent, held, workers = transaction.totals()
            events = transaction.events()
            calls = [self._public_call(row) for row in transaction.calls()]
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
        return publish_checkpoint(self.snapshot(), destination, self.ledger)
