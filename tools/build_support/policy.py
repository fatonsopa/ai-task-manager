"""Build policy values and canonical evidence bindings, without storage or execution."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from typing import Any

from .values import ControlError, _money, _require, _utc


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
    return {
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


def lifted_policy(
    authorization: dict[str, Any],
    authorization_sha: str,
    matrix: dict[str, Any],
    matrix_sha: str,
    revision: int,
) -> dict[str, Any]:
    _require(
        authorization.get("origin") == "user" and bool(authorization.get("instruction")),
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
    _require(
        isinstance(authorization.get("id"), str) and authorization["id"],
        "invalid_authorization",
        "The recorded authorization needs an identity.",
    )
    recorded_at = _utc(authorization.get("recorded_at_utc")).isoformat()
    profiles = matrix.get("selected_profiles", [])
    _require(
        isinstance(profiles, list)
        and matrix.get("minimum_models_per_host") == 2
        and matrix.get("excluded_model_family", "").casefold() == "fable",
        "invalid_authorization",
        "The pinned two-model matrix and exclusion are required.",
    )
    keys: set[tuple[str, str, str]] = set()
    for profile in profiles:
        _require(isinstance(profile, dict), "invalid_authorization", "Invalid profile.")
        key = tuple(profile.get(k) for k in ("host", "model", "reasoning_effort"))
        _require(
            all(isinstance(v, str) and v for v in key)
            and key[0] in {"claude-code", "codex-cli"}
            and "fable" not in key[1].casefold()
            and key not in keys,
            "invalid_authorization",
            "Profiles must be distinct, explicit and permitted.",
        )
        keys.add(key)
    _require(
        all(
            len({model for h, model, _ in keys if h == host}) >= 2
            for host in ("claude-code", "codex-cli")
        ),
        "invalid_authorization",
        "At least two models on each host are required.",
    )
    return {
        "format": 1,
        "revision": revision,
        "max_incremental_spend_micro": None,
        "deadline": None,
        "max_elapsed_seconds": None,
        "max_total_retry_rounds": None,
        "authorization_id": authorization["id"],
        "authorization_sha256": authorization_sha,
        "authorization_recorded_at": recorded_at,
        "matrix_sha256": matrix_sha,
        "required_profiles": [
            dict(zip(("host", "model", "reasoning_effort"), k)) for k in sorted(keys)
        ],
    }


def validate_policy(value: dict[str, Any], expected_digest: str) -> dict[str, Any]:
    _require(
        isinstance(value, dict)
        and value.get("format") == 1
        and type(value.get("revision")) is int
        and value["revision"] >= 0
        and digest(value) == expected_digest,
        "invalid_ledger",
        "Unsupported or corrupt build policy.",
    )
    cap = value.get("max_incremental_spend_micro")
    _require(
        cap is None or type(cap) is int and cap > 0, "invalid_ledger", "Invalid policy budget."
    )
    if value.get("deadline") is not None:
        _utc(value["deadline"])
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
