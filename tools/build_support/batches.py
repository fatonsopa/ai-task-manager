"""Finite reviewed execution batches and invocation bindings, with no total retry cap."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .amendments import require_current
from .authorization import RecordVerifier
from .policy import batch_definition, encoded, finite_seconds
from .storage import SCHEMA_VERSION, LedgerStore, LedgerTransaction
from .values import _identifier, _require, _text, _utc


def ref_sha(ref: str | None, kind: str) -> str | None:
    if ref is None:
        return None
    _require(
        isinstance(ref, str) and re.fullmatch(kind + r":[0-9a-f]{64}", ref) is not None,
        "invalid_authorization",
        "Records need content-bound typed references.",
    )
    return ref.split(":", 1)[1]


def register(
    store: LedgerStore,
    clock: Callable[[], datetime],
    records: RecordVerifier,
    *,
    expected_revision: int,
    batch_id: str,
    max_invocations: int,
    max_invocation_seconds: float,
    max_incremental_spend_usd: Any,
    review_ref: str,
    reason: str,
    continuation_of: str | None = None,
    checkpoint_ref: str | None = None,
) -> dict[str, Any]:
    _identifier(batch_id, "batch_id")
    _text(reason, "reviewed batch reason")
    definition = batch_definition(
        batch_id,
        expected_revision,
        max_invocations,
        max_invocation_seconds,
        max_incremental_spend_usd,
        continuation_of,
        ref_sha(checkpoint_ref, "checkpoint"),
        reason,
    )
    review_sha = ref_sha(review_ref, "batch_review")
    with store.transaction() as transaction:
        run = transaction.run()
        _require(
            transaction.schema_version() == SCHEMA_VERSION,
            "invalid_transition",
            "Reviewed batches require the amended schema.",
        )
        prior = transaction.batch(batch_id)
        if prior:
            _require(
                prior["definition"] == encoded(definition) and prior["review_sha256"] == review_sha,
                "batch_conflict",
                "Batch identifier has different inputs.",
            )
            return json.loads(prior["receipt"])
        require_current(transaction, expected_revision, admission=True)
        if continuation_of is not None:
            _identifier(continuation_of, "continuation_of")
            _require(
                transaction.batch(continuation_of) is not None,
                "unknown_batch",
                "Continuation needs its actual prior batch.",
            )
            checkpoint = records.resolve(checkpoint_ref, "checkpoint", run["run_id"])
            _require(
                checkpoint.data.get("run_id") == run["run_id"]
                and checkpoint.data.get("ledger_digest") == transaction.state_digest()
                and any(
                    b.get("batch_id") == continuation_of for b in checkpoint.data.get("batches", [])
                ),
                "stale_checkpoint",
                "Continuation needs a current scoped prior-batch checkpoint.",
            )
        else:
            _require(
                checkpoint_ref is None, "invalid_input", "An initial batch has no prior checkpoint."
            )
        review = records.resolve(review_ref, "batch_review", run["run_id"])
        _require(
            review.sha256 == review_sha
            and review.data.get("run_id") == run["run_id"]
            and review.data.get("definition") == definition,
            "invalid_authorization",
            "The actual review does not match this batch.",
        )
        receipt = {
            "batch_id": batch_id,
            "definition": definition,
            "review_sha256": review.sha256,
            "registered_at": _utc(clock()).isoformat(),
        }
        transaction.insert_batch(definition, review.sha256, receipt)
        transaction.append_event("batch_registered", None, receipt["registered_at"], receipt)
        return receipt


def check_invocation(
    transaction: LedgerTransaction, batch_id: str | None, timeout_seconds: Any, maximum: int
) -> tuple[str, float]:
    _require(isinstance(batch_id, str), "batch_required", "A reviewed batch is required.")
    timeout = finite_seconds(timeout_seconds)
    row = transaction.batch(batch_id)
    _require(row is not None, "unknown_batch", "The reviewed batch does not exist.")
    definition = json.loads(row["definition"])
    _require(
        definition["policy_revision"] == transaction.policy()["revision"],
        "stale_policy",
        "A batch cannot authorize work against a different policy.",
    )
    _require(
        timeout <= definition["max_invocation_seconds"],
        "invocation_bound_exceeded",
        "Invocation timeout exceeds its reviewed batch bound.",
    )
    count, held = transaction.batch_totals(batch_id)
    _require(
        count < definition["max_invocations"],
        "batch_exhausted",
        "Batch invocation allowance is used.",
    )
    _require(
        held + maximum <= definition["max_incremental_spend_micro"],
        "batch_budget_exceeded",
        "Batch spending allowance is reserved.",
    )
    return batch_id, timeout


def bind_legacy(
    store: LedgerStore,
    clock: Callable[[], datetime],
    records: RecordVerifier,
    *,
    expected_revision: int,
    call_id: str,
    batch_id: str,
    timeout_seconds: float,
    evidence_ref: str,
) -> dict[str, Any]:
    request = {
        "call_id": call_id,
        "batch_id": batch_id,
        "timeout_seconds": finite_seconds(timeout_seconds),
        "policy_revision": expected_revision,
    }
    with store.transaction() as transaction:
        run = require_current(transaction, expected_revision, admission=True)
        _require(
            transaction.schema_version() == SCHEMA_VERSION,
            "invalid_transition",
            "Legacy binding requires the amended schema.",
        )
        prior = transaction.bound(call_id)
        if prior:
            _require(
                prior["batch_id"] == batch_id
                and prior["timeout_seconds"] == request["timeout_seconds"]
                and prior["binding_sha256"] == ref_sha(evidence_ref, "invocation_binding"),
                "call_conflict",
                "This invocation already has different bounds.",
            )
            return prior
        call = transaction.call(call_id)
        _require(
            call["status"] == "reserved",
            "invalid_transition",
            "Do not restart an active legacy call.",
        )
        record = records.resolve(evidence_ref, "invocation_binding", run["run_id"])
        _require(
            record.data.get("run_id") == run["run_id"] and record.data.get("binding") == request,
            "invalid_authorization",
            "The actual review does not bind this legacy invocation.",
        )
        batch, timeout = check_invocation(
            transaction, batch_id, timeout_seconds, call["reserved_micro"]
        )
        transaction.insert_bound(call_id, batch, timeout, expected_revision, record.sha256)
        transaction.append_event(
            "legacy_invocation_bound", call_id, _utc(clock()).isoformat(), request
        )
        return transaction.bound(call_id)
