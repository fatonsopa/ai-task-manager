"""LP13 malformed-policy format controls, including correctly hashed corruption."""

import copy
import json
import sqlite3

import pytest

from tests.test_build_control_policy_amendment import raw, refused
from tests.test_build_control_policy_amendment import rig as rig
from tools.build_control import ControlError
from tools.build_support.policy import baseline, digest, lifted_policy, validate_policy

FIELDS = (
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
)
PROFILES = [
    {"host": host, "model": model, "reasoning_effort": "high"}
    for host, model in [
        ("claude-code", "claude-opus-5-5"),
        ("claude-code", "claude-sonnet-5-5"),
        ("codex-cli", "gpt-6.1-sol"),
        ("codex-cli", "gpt-6-astra"),
    ]
]
BASELINE = {
    "format": 1,
    "revision": 0,
    "max_incremental_spend_micro": 100_000_000,
    "deadline": "2026-11-03T12:00:00+00:00",
    "max_elapsed_seconds": 172800,
    "max_total_retry_rounds": None,
    "authorization_id": None,
    "authorization_sha256": None,
    "authorization_recorded_at": None,
    "matrix_sha256": None,
    "required_profiles": [],
}
AMENDED = {
    "format": 1,
    "revision": 1,
    "max_incremental_spend_micro": None,
    "deadline": None,
    "max_elapsed_seconds": None,
    "max_total_retry_rounds": None,
    "authorization_id": "fixture-authorization",
    "authorization_sha256": "a" * 64,
    "authorization_recorded_at": "2026-11-01T13:00:00+00:00",
    "matrix_sha256": "b" * 64,
    "required_profiles": PROFILES,
}


def malformed_policies():
    cases = [("missing-fields", {"format": 1, "revision": 1})]
    for name, key, value in [
        ("format-bool", "format", True),
        ("format-float", "format", 1.0),
        ("unknown-format", "format", 2),
        ("revision-bool", "revision", True),
        ("revision-float", "revision", 1.0),
        ("negative-revision", "revision", -1),
        ("unknown-field", "reviewed", True),
    ]:
        cases.append((name, copy.deepcopy(AMENDED) | {key: value}))
    for label, valid in [("baseline", BASELINE), ("amended", AMENDED)]:
        for field in FIELDS:
            value = copy.deepcopy(valid)
            value.pop(field)
            cases.append((label + "-missing-" + field, value))
    for name, key, bad in [
        ("cap-null", "max_incremental_spend_micro", None),
        ("cap-bool", "max_incremental_spend_micro", True),
        ("cap-zero", "max_incremental_spend_micro", 0),
        ("cap-negative", "max_incremental_spend_micro", -1),
        ("cap-over-legacy", "max_incremental_spend_micro", 100_000_001),
        ("cap-string", "max_incremental_spend_micro", "100"),
        ("deadline-null", "deadline", None),
        ("deadline-naive", "deadline", "2026-11-03T12:00:00"),
        ("deadline-invalid", "deadline", "unknown"),
        ("deadline-noncanonical", "deadline", "2026-11-03T13:00:00+01:00"),
        ("elapsed-null", "max_elapsed_seconds", None),
        ("elapsed-bool", "max_elapsed_seconds", True),
        ("elapsed-zero", "max_elapsed_seconds", 0),
        ("elapsed-negative", "max_elapsed_seconds", -1),
        ("elapsed-over-legacy", "max_elapsed_seconds", 172801),
        ("elapsed-float", "max_elapsed_seconds", 172800.0),
        ("retry-number", "max_total_retry_rounds", 5),
        ("authorization-present", "authorization_id", "unexpected-authority"),
        ("profiles-present", "required_profiles", PROFILES),
    ]:
        cases.append(("baseline-" + name, copy.deepcopy(BASELINE) | {key: bad}))
    for name, key, bad in [
        ("cap-number", "max_incremental_spend_micro", 100_000_000),
        ("elapsed-number", "max_elapsed_seconds", 172800),
        ("deadline-present", "deadline", BASELINE["deadline"]),
        ("retry-number", "max_total_retry_rounds", 5),
        ("authorization-null", "authorization_id", None),
        ("authorization-empty", "authorization_id", ""),
        ("authorization-bool", "authorization_id", True),
        ("authorization-invalid", "authorization_id", "not a bounded identity"),
        ("authorization-sha-null", "authorization_sha256", None),
        ("authorization-sha-short", "authorization_sha256", "a" * 63),
        ("authorization-sha-invalid", "authorization_sha256", "g" * 64),
        ("recorded-null", "authorization_recorded_at", None),
        ("recorded-bool", "authorization_recorded_at", True),
        ("recorded-naive", "authorization_recorded_at", "2026-11-01T13:00:00"),
        ("matrix-sha-null", "matrix_sha256", None),
        ("matrix-sha-invalid", "matrix_sha256", "g" * 64),
        ("profiles-null", "required_profiles", None),
        ("profiles-empty", "required_profiles", []),
        ("profiles-missing", "required_profiles", PROFILES[:3]),
        ("profiles-duplicate", "required_profiles", [PROFILES[0]] * 4),
        ("profiles-wrong-type", "required_profiles", True),
    ]:
        cases.append(("amended-" + name, copy.deepcopy(AMENDED) | {key: bad}))
    for name, key, bad in [
        ("foreign-host", "host", "foreign"),
        ("fable", "model", "Fable"),
        ("medium", "reasoning_effort", "medium"),
        ("extra-field", "reviewed", True),
        ("unknown-model", "model", "unselected-model"),
    ]:
        value = copy.deepcopy(AMENDED)
        value["required_profiles"][0][key] = bad
        cases.append(("amended-profiles-" + name, value))
    value = copy.deepcopy(AMENDED)
    value["required_profiles"][0].pop("reasoning_effort")
    cases.append(("amended-profiles-missing-effort", value))
    return cases


@pytest.mark.parametrize(
    "name,value", malformed_policies(), ids=lambda value: value if isinstance(value, str) else None
)
def test_lp13_complete_policy_format(name, value):
    original = copy.deepcopy(value)
    # A correct digest deliberately cannot substitute for format validation.
    refused("invalid_ledger", validate_policy, value, digest(value))
    assert value == original


def test_lp13_valid_baseline_and_amended_round_trip():
    for value in [BASELINE, AMENDED, AMENDED | {"revision": 2}]:
        source = copy.deepcopy(value)
        assert validate_policy(source, digest(source)) is source
        assert source == value
    original_run = {
        "budget_micro": 100_000_000,
        "deadline": BASELINE["deadline"],
        "started_at": "2026-11-01T12:00:00+00:00",
    }
    assert baseline(original_run) == BASELINE


@pytest.mark.parametrize(
    "variant", ["baseline-cap", "authorization-sha", "matrix-sha", "medium-profile"]
)
def test_lp13_creation_uses_the_shared_policy_contract(variant):
    if variant == "baseline-cap":
        with pytest.raises(ControlError):
            baseline(
                {
                    "budget_micro": 0,
                    "deadline": BASELINE["deadline"],
                    "started_at": "2026-11-01T12:00:00+00:00",
                }
            )
        return
    authority = {
        "origin": "user",
        "instruction": "Authored fixture only",
        "effective_total_limits": {
            "max_incremental_spend_usd": None,
            "max_elapsed_seconds": None,
            "max_total_retry_rounds": None,
        },
        "id": AMENDED["authorization_id"],
        "recorded_at_utc": AMENDED["authorization_recorded_at"],
    }
    matrix = {
        "minimum_models_per_host": 2,
        "excluded_model_family": "Fable",
        "selected_profiles": copy.deepcopy(PROFILES),
    }
    if variant == "medium-profile":
        matrix["selected_profiles"][0]["reasoning_effort"] = "medium"
    with pytest.raises(ControlError):
        lifted_policy(
            authority,
            "bad" if variant == "authorization-sha" else "a" * 64,
            matrix,
            "bad" if variant == "matrix-sha" else "b" * 64,
            1,
        )


@pytest.mark.parametrize("variant", ["missing-fields", "format-bool", "amended-profiles-medium"])
def test_lp13_persisted_hash_valid_incomplete_policy_refuses(rig, variant):
    rig.amend()
    value = dict(malformed_policies())[variant]
    with sqlite3.connect(rig.base.ledger) as connection:
        connection.execute(
            "UPDATE policy_revision SET data=?,policy_digest=? WHERE revision=1",
            (json.dumps(value), digest(value)),
        )
    before = raw(rig.base.ledger)
    refused("invalid_ledger", rig.control().snapshot)
    assert raw(rig.base.ledger) == before
