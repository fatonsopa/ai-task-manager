"""Build policy values and canonical evidence bindings, without storage or execution."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import timedelta
from typing import Any

from .values import (
    IDENTIFIER,
    MAX_BUDGET_MICRO,
    MAX_SECONDS,
    ControlError,
    _money,
    _require,
    _utc,
)

POLICY_FIELDS = frozenset(
    {
        "format",
        "revision",
        "max_incremental_spend_micro",
        "deadline",
        "max_elapsed_seconds",
        "max_total_retry_rounds",
        "authorization_id",
        "authorization_sha256",
        "authorization_recorded_at",
        "matrix_sha256",
        "required_profiles",
    }
)
PROFILE_FIELDS = ("host", "model", "reasoning_effort")
# Format 1 implements the current explicitly selected matrix. A later matrix
# needs a versioned authorized contract, not silent acceptance of other profiles.
FORMAT1_PROFILES = frozenset(
    {
        ("claude-code", "claude-opus-5-5", "high"),
        ("claude-code", "claude-sonnet-5-5", "high"),
        ("codex-cli", "gpt-6.1-sol", "high"),
        ("codex-cli", "gpt-6-astra", "high"),
    }
)


def encoded(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ControlError("invalid_input", "Use finite JSON operation inputs.") from error


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def finite_seconds(value: Any) -> float:
    _require(
        type(value) in (int, float) and math.isfinite(value) and value > 0,
        "invalid_input",
        "An invocation needs a positive finite timeout.",
    )
    try:
        timedelta(seconds=value)
    except OverflowError as error:
        raise ControlError("invalid_input", "Timeout exceeds representable duration.") from error
    return float(value)


def finite_count(value: Any) -> int:
    _require(
        type(value) is int and 0 < value <= 1_000_000,
        "invalid_input",
        "A batch needs a positive finite invocation count.",
    )
    return value


def baseline(run: dict[str, Any]) -> dict[str, Any]:
    value = {
        "format": 1,
        "revision": 0,
        "max_incremental_spend_micro": run["budget_micro"],
        "deadline": run["deadline"],
        "max_elapsed_seconds": int(
            (_utc(run["deadline"]) - _utc(run["started_at"])).total_seconds()
        ),
        "max_total_retry_rounds": None,
        "authorization_id": None,
        "authorization_sha256": None,
        "authorization_recorded_at": None,
        "matrix_sha256": None,
        "required_profiles": [],
    }
    return validate_policy(value, digest(value))


def lifted_policy(
    authorization: dict[str, Any],
    authorization_sha: str,
    matrix: dict[str, Any],
    matrix_sha: str,
    revision: int,
) -> dict[str, Any]:
    _require(
        authorization.get("origin") == "user"
        and isinstance(authorization.get("instruction"), str)
        and bool(authorization["instruction"].strip()),
        "invalid_authorization",
        "A pinned recorded user instruction is required.",
    )
    expected = {
        "max_incremental_spend_usd": None,
        "max_elapsed_seconds": None,
        "max_total_retry_rounds": None,
    }
    _require(
        authorization.get("effective_total_limits") == expected,
        "invalid_authorization",
        "This amendment only implements the recorded lifted totals.",
    )
    try:
        recorded_at = _utc(authorization.get("recorded_at_utc")).isoformat()
    except ControlError as error:
        raise ControlError("invalid_authorization", "Invalid authorization timestamp.") from error
    profiles = matrix.get("selected_profiles")
    _require(
        isinstance(profiles, list)
        and type(matrix.get("minimum_models_per_host")) is int
        and matrix.get("minimum_models_per_host") == 2
        and isinstance(matrix.get("excluded_model_family"), str)
        and matrix.get("excluded_model_family", "").casefold() == "fable",
        "invalid_authorization",
        "The pinned two-model matrix and exclusion are required.",
    )
    try:
        # Source records may include discovery notes; persisted policy has only
        # the three canonical profile fields validated below.
        normalized = [{field: profile[field] for field in PROFILE_FIELDS} for profile in profiles]
    except (KeyError, TypeError) as error:
        raise ControlError("invalid_authorization", "Incomplete source profile.") from error
    value = {
        "format": 1,
        "revision": revision,
        "max_incremental_spend_micro": None,
        "deadline": None,
        "max_elapsed_seconds": None,
        "max_total_retry_rounds": None,
        "authorization_id": authorization.get("id"),
        "authorization_sha256": authorization_sha,
        "authorization_recorded_at": recorded_at,
        "matrix_sha256": matrix_sha,
        "required_profiles": normalized,
    }
    try:
        validate_policy(value, digest(value))
    except ControlError as error:
        raise ControlError("invalid_authorization", "Invalid authorized policy format.") from error
    value["required_profiles"].sort(key=lambda profile: tuple(profile[k] for k in PROFILE_FIELDS))
    return value


def _canonical_utc(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return _utc(value).isoformat() == value
    except ControlError:
        return False


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _format1_profiles(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != len(FORMAT1_PROFILES):
        return False
    if not all(
        isinstance(profile, dict)
        and set(profile) == set(PROFILE_FIELDS)
        and all(isinstance(profile[field], str) for field in PROFILE_FIELDS)
        for profile in value
    ):
        return False
    keys = [tuple(profile[field] for field in PROFILE_FIELDS) for profile in value]
    return len(set(keys)) == len(keys) and set(keys) == FORMAT1_PROFILES


def validate_policy(value: dict[str, Any], expected_digest: str) -> dict[str, Any]:
    _require(
        isinstance(value, dict)
        and set(value) == POLICY_FIELDS
        and type(value["format"]) is int
        and value["format"] == 1
        and type(value["revision"]) is int
        and value["revision"] >= 0,
        "invalid_ledger",
        "Unsupported or incomplete build policy format.",
    )
    try:
        actual_digest = digest(value)
    except ControlError as error:
        raise ControlError("invalid_ledger", "Invalid policy JSON values.") from error
    _require(
        _sha256(expected_digest) and actual_digest == expected_digest,
        "invalid_ledger",
        "Corrupt build policy digest.",
    )
    _require(
        value["max_total_retry_rounds"] is None,
        "invalid_ledger",
        "Unsupported total retry ceiling.",
    )
    if value["revision"] == 0:
        _require(
            type(value["max_incremental_spend_micro"]) is int
            and 0 < value["max_incremental_spend_micro"] <= MAX_BUDGET_MICRO
            and type(value["max_elapsed_seconds"]) is int
            and 0 < value["max_elapsed_seconds"] <= MAX_SECONDS
            and _canonical_utc(value["deadline"])
            and all(
                value[field] is None
                for field in (
                    "authorization_id",
                    "authorization_sha256",
                    "authorization_recorded_at",
                    "matrix_sha256",
                )
            )
            and value["required_profiles"] == [],
            "invalid_ledger",
            "Invalid legacy baseline policy relationships.",
        )
    else:
        _require(
            all(
                value[field] is None
                for field in ("max_incremental_spend_micro", "max_elapsed_seconds", "deadline")
            )
            and isinstance(value["authorization_id"], str)
            and IDENTIFIER.fullmatch(value["authorization_id"]) is not None
            and _sha256(value["authorization_sha256"])
            and _sha256(value["matrix_sha256"])
            and _canonical_utc(value["authorization_recorded_at"])
            and _format1_profiles(value["required_profiles"]),
            "invalid_ledger",
            "Invalid amended policy relationships or metadata.",
        )
    return value


def batch_definition(
    batch_id: str,
    revision: int,
    max_invocations: int,
    max_invocation_seconds: float,
    max_incremental_spend_usd: Any,
    continuation_of: str | None,
    checkpoint_sha: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "policy_revision": revision,
        "max_invocations": finite_count(max_invocations),
        "max_invocation_seconds": finite_seconds(max_invocation_seconds),
        "max_incremental_spend_micro": _money(max_incremental_spend_usd),
        "continuation_of": continuation_of,
        "checkpoint_sha256": checkpoint_sha,
        "reason": reason,
    }
