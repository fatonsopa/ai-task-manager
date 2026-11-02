"""Explicit policy migration and compatible recovery over the storage interface."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .authorization import RecordVerifier
from .checkpoint import publish_checkpoint
from .policy import digest, lifted_policy
from .storage import SCHEMA_VERSION, LedgerStore, LedgerTransaction
from .values import _identifier, _require, _text, _utc


def require_current(
    transaction: LedgerTransaction, expected_revision: int | None, *, admission: bool = False
) -> dict[str, Any]:
    run = transaction.run()
    policy = transaction.policy()
    if transaction.schema_version() == SCHEMA_VERSION:
        _require(
            type(expected_revision) is int and expected_revision == policy["revision"],
            "stale_policy",
            "Bind mutations to the current policy revision explicitly.",
        )
        if admission:
            _require(not transaction.paused(), "admission_paused", "New admission is paused.")
    return run


def prepare(
    store: LedgerStore, clock: Callable[[], datetime], backup_path: str | Path
) -> dict[str, Any]:
    with store.transaction() as transaction:
        run = transaction.run()
        policy = transaction.policy()
        backup = transaction.backup_to(Path(backup_path))
        result = {
            "format": 1,
            "run_id": run["run_id"],
            "started_at": run["started_at"],
            "policy_revision": policy["revision"],
            "policy_digest": digest(policy),
            "ledger_digest": transaction.state_digest(),
            "backup": backup,
            "prepared_at": _utc(clock()).isoformat(),
        }
        output = Path(str(backup_path) + ".preparation.json")
        _require(
            not output.exists() and not output.is_symlink(),
            "invalid_input",
            "Preparation evidence must have a new path.",
        )
        publication = publish_checkpoint(result, output, store.path)
    return {
        **result,
        "preparation": publication,
        "preparation_ref": "preparation:" + publication["sha256"],
    }


def apply(
    store: LedgerStore,
    clock: Callable[[], datetime],
    records: RecordVerifier,
    *,
    amendment_id: str,
    expected_run_id: str,
    expected_policy_revision: int,
    expected_policy_digest: str,
    expected_ledger_digest: str,
    preparation_ref: str,
    authorization_ref: str,
    model_matrix_ref: str,
) -> dict[str, Any]:
    _identifier(amendment_id, "amendment_id")
    request = {
        "amendment_id": amendment_id,
        "expected_run_id": expected_run_id,
        "expected_policy_revision": expected_policy_revision,
        "expected_policy_digest": expected_policy_digest,
        "expected_ledger_digest": expected_ledger_digest,
        "preparation_ref": preparation_ref,
        "authorization_ref": authorization_ref,
        "model_matrix_ref": model_matrix_ref,
    }
    request_digest = digest(request)
    with store.transaction() as transaction:
        run = transaction.run()
        existing = transaction.amendment(amendment_id)
        if existing:
            _require(
                existing["request_digest"] == request_digest,
                "amendment_conflict",
                "Amendment identifier has different inputs.",
            )
            return json.loads(existing["receipt"])
        current = transaction.policy()
        _require(run["run_id"] == expected_run_id, "run_conflict", "Wrong build run.")
        _require(
            type(expected_policy_revision) is int
            and current["revision"] == expected_policy_revision
            and digest(current) == expected_policy_digest,
            "stale_policy",
            "The expected policy changed.",
        )
        _require(
            transaction.state_digest() == expected_ledger_digest,
            "stale_preparation",
            "The prepared ledger state changed.",
        )
        _require(
            not any(c["status"] == "launching" for c in transaction.calls()),
            "launch_unresolved",
            "Reconcile pending launch intents before migration.",
        )
        prepared = records.resolve(preparation_ref, "preparation", run["run_id"])
        before = prepared.data
        _require(
            before.get("format") == 1
            and before.get("run_id") == run["run_id"]
            and before.get("started_at") == run["started_at"]
            and before.get("policy_revision") == expected_policy_revision
            and before.get("policy_digest") == expected_policy_digest
            and before.get("ledger_digest") == expected_ledger_digest,
            "stale_preparation",
            "Preparation evidence does not match this state.",
        )
        backup = before.get("backup", {})
        _require(
            isinstance(backup, dict) and backup.get("ledger_digest") == expected_ledger_digest,
            "backup_conflict",
            "A matching durable backup is required.",
        )
        store.validate_backup(
            backup.get("path", ""), backup.get("sha256", ""), expected_ledger_digest
        )
        authority = records.resolve(authorization_ref, "user_authorization", run["run_id"])
        matrix = records.resolve(model_matrix_ref, "model_matrix", run["run_id"])
        policy = lifted_policy(
            authority.data, authority.sha256, matrix.data, matrix.sha256, current["revision"] + 1
        )
        _require(
            not transaction.authorization_used(policy["authorization_id"], authority.sha256),
            "stale_authorization",
            "This authorization already has an amendment.",
        )
        previous_time = current.get("authorization_recorded_at")
        _require(
            previous_time is None
            or _utc(policy["authorization_recorded_at"]) > _utc(previous_time),
            "stale_authorization",
            "An older authorization cannot replace the current policy.",
        )
        at = _utc(clock()).isoformat()
        receipt = {
            "amendment_id": amendment_id,
            "request_digest": request_digest,
            "run_id": run["run_id"],
            "started_at": run["started_at"],
            "before_ledger_digest": expected_ledger_digest,
            "before_policy_digest": expected_policy_digest,
            "policy_revision": policy["revision"],
            "policy_digest": digest(policy),
            "authorization_id": policy["authorization_id"],
            "authorization_sha256": authority.sha256,
            "matrix_sha256": matrix.sha256,
            "backup": backup,
            "committed_at": at,
            "provenance": (
                "Pinned lead-recorded native user instruction; not cryptographic origin proof"
            ),
        }
        transaction.upgrade_policy_schema()
        transaction.insert_policy(policy, expected_policy_digest)
        transaction.append_event("policy_amended", None, at, receipt)
        transaction.insert_amendment(
            amendment_id, request_digest, policy["authorization_id"], authority.sha256, receipt
        )
        return receipt


def admission(
    store: LedgerStore,
    clock: Callable[[], datetime],
    records: RecordVerifier,
    *,
    operation_id: str,
    expected_revision: int,
    paused: bool,
    review_ref: str,
    checkpoint_ref: str | None = None,
) -> dict[str, Any]:
    _identifier(operation_id, "operation_id")
    _require(type(paused) is bool, "invalid_input", "Use an explicit paused state.")
    request = {
        "operation_id": operation_id,
        "expected_revision": expected_revision,
        "paused": paused,
        "review_ref": review_ref,
        "checkpoint_ref": checkpoint_ref,
    }
    request_digest = digest(request)
    with store.transaction() as transaction:
        run = transaction.run()
        _require(
            transaction.schema_version() == SCHEMA_VERSION,
            "invalid_transition",
            "Admission recovery requires the amended schema.",
        )
        prior = transaction.operation(operation_id)
        if prior:
            _require(
                prior["kind"] == "admission" and prior["request_digest"] == request_digest,
                "operation_conflict",
                "Recovery identifier has different inputs.",
            )
            return json.loads(prior["receipt"])
        require_current(transaction, expected_revision)
        review = records.resolve(review_ref, "admission_review", run["run_id"])
        checkpoint_sha = None
        if not paused:
            checkpoint = records.resolve(checkpoint_ref, "checkpoint", run["run_id"])
            _require(
                checkpoint.data.get("run_id") == run["run_id"]
                and checkpoint.data.get("ledger_digest") == transaction.state_digest(),
                "stale_checkpoint",
                "Resume needs a current scoped checkpoint.",
            )
            checkpoint_sha = checkpoint.sha256
        expected = {
            "operation_id": operation_id,
            "run_id": run["run_id"],
            "policy_revision": expected_revision,
            "paused": paused,
            "checkpoint_sha256": checkpoint_sha,
        }
        _require(
            all(review.data.get(k) == v for k, v in expected.items()),
            "invalid_authorization",
            "Recovery review does not match this operation.",
        )
        _text(review.data.get("reason"), "reviewed recovery reason")
        receipt = {**expected, "review_sha256": review.sha256, "at": _utc(clock()).isoformat()}
        transaction.set_paused(paused)
        transaction.append_event(
            "admission_paused" if paused else "admission_resumed", None, receipt["at"], receipt
        )
        transaction.insert_operation(operation_id, "admission", request_digest, receipt)
        return receipt
