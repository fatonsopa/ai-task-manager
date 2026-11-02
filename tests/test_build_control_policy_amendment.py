"""Authored policy/migration controls; no live user, model or provider effects."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tools.build_control import BuildControl, ControlError
from tools.build_support.authorization import PinnedRecords
from tools.build_support.policy import digest

START = datetime(2026, 11, 1, 12, tzinfo=UTC)
ROOT = Path(__file__).parents[1]


class Rig:
    def __init__(self, root):
        self.root = root
        self.now = START
        self.bindings = {}
        self.number = 0
        self.base = BuildControl(root / "ledger.sqlite", clock=lambda: self.now)
        self.base.initialize(run_id="policy-fixture", started_at=START, worker_cap=10)
        self.user = self.pin(
            "user_authorization",
            {
                "origin": "user",
                "id": "fixture-auth-1",
                "instruction": "Authored lifted-limit control",
                "recorded_at_utc": (START + timedelta(minutes=1)).isoformat(),
                "effective_total_limits": {
                    "max_incremental_spend_usd": None,
                    "max_elapsed_seconds": None,
                    "max_total_retry_rounds": None,
                },
            },
        )
        self.matrix = self.pin(
            "model_matrix",
            {
                "minimum_models_per_host": 2,
                "excluded_model_family": "Fable",
                "selected_profiles": [
                    {"host": h, "model": m, "reasoning_effort": "high"}
                    for h, m in [
                        ("claude-code", "claude-opus-5-5"),
                        ("claude-code", "claude-sonnet-5-5"),
                        ("codex-cli", "gpt-6.1-sol"),
                        ("codex-cli", "gpt-6-astra"),
                    ]
                ],
            },
        )
        self.current = None

    def pin_file(self, kind, path, run="policy-fixture"):
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        ref = kind + ":" + sha
        self.bindings[ref] = {"kind": kind, "run_id": run, "path": str(path), "sha256": sha}
        return ref

    def pin(self, kind, data, run="policy-fixture"):
        self.number += 1
        path = self.root / f"record-{self.number}.json"
        path.write_text(json.dumps(data, sort_keys=True) + "\n")
        path.chmod(0o600)
        return self.pin_file(kind, path, run)

    def control(self, revision=None):
        return BuildControl(
            self.base.ledger,
            clock=lambda: self.now,
            expected_policy_revision=revision,
            records=PinnedRecords(self.bindings),
        )

    def prepare(self):
        self.number += 1
        prepared = self.control().prepare_policy_amendment(
            self.root / f"backup-{self.number}.sqlite"
        )
        ref = self.pin_file("preparation", Path(prepared["preparation"]["path"]))
        return {
            "amendment_id": "amend-1",
            "expected_run_id": "policy-fixture",
            "expected_policy_revision": prepared["policy_revision"],
            "expected_policy_digest": prepared["policy_digest"],
            "expected_ledger_digest": prepared["ledger_digest"],
            "preparation_ref": ref,
            "authorization_ref": self.user,
            "model_matrix_ref": self.matrix,
        }

    def amend(self, request=None):
        request = request or self.prepare()
        receipt = self.control().amend_policy(**request)
        self.current = self.control(receipt["policy_revision"])
        return receipt

    def checkpoint(self):
        self.number += 1
        path = self.root / f"checkpoint-{self.number}.json"
        self.current.checkpoint(path)
        return self.pin_file("checkpoint", path)

    def batch(self, name="batch", count=20, seconds=5, budget="1000", parent=None, checkpoint=None):
        revision = self.current.snapshot()["policy"]["revision"]
        definition = {
            "batch_id": name,
            "policy_revision": revision,
            "max_invocations": count,
            "max_invocation_seconds": float(seconds),
            "max_incremental_spend_micro": int(Decimal(budget) * 1_000_000),
            "continuation_of": parent,
            "checkpoint_sha256": checkpoint.split(":")[1] if checkpoint else None,
            "reason": "Authored bounded regression batch",
        }
        review = self.pin("batch_review", {"run_id": "policy-fixture", "definition": definition})
        request = {
            "expected_revision": revision,
            "batch_id": name,
            "max_invocations": count,
            "max_invocation_seconds": seconds,
            "max_incremental_spend_usd": budget,
            "review_ref": review,
            "reason": definition["reason"],
            "continuation_of": parent,
            "checkpoint_ref": checkpoint,
        }
        self.current = self.control(revision)
        return self.current.register_batch(**request), request

    def reserve(self, name="call", cost=None, timeout=2, worker=True, batch="batch"):
        return self.current.reserve(
            name,
            label="authored fixture",
            paid=cost is not None,
            max_cost_usd=cost,
            batch_id=batch,
            timeout_seconds=timeout,
            uses_worker=worker,
        )

    def admission(self, paused, name=None):
        name = name or ("pause" if paused else "resume")
        checkpoint = None if paused else self.checkpoint()
        revision = self.current.expected_policy_revision
        review = self.pin(
            "admission_review",
            {
                "operation_id": name,
                "run_id": "policy-fixture",
                "policy_revision": revision,
                "paused": paused,
                "checkpoint_sha256": checkpoint.split(":")[1] if checkpoint else None,
                "reason": "Authored recovery evidence",
            },
        )
        request = {
            "operation_id": name,
            "expected_revision": revision,
            "review_ref": review,
            "checkpoint_ref": checkpoint,
        }
        self.current = self.control(revision)
        function = self.current.pause_admission if paused else self.current.resume_admission
        return function(**request), request


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def raw(path):
    with sqlite3.connect(path) as connection:
        names = [
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            "version": connection.execute("PRAGMA user_version").fetchone()[0],
            "tables": {
                name: connection.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
                for name in names
            },
        }


def refused(code, function, *args, **kwargs):
    with pytest.raises(ControlError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def test_lp02_14_original_rows_are_unchanged(rig):
    for name, outcome in [
        ("success", "succeeded"),
        ("timeout", "interrupted"),
        ("unknown", "unknown"),
    ]:
        rig.base.reserve(name, label=name, paid=True, max_cost_usd="10")
        rig.base.mark_started(name, worker_identity=name)
        rig.base.record_outcome(
            name,
            outcome=outcome,
            process_stopped=outcome != "unknown",
            evidence_ref="authored-observation",
            reason="retained failure",
        )
    rig.base.reserve("reserved", label="reserved", paid=False, max_cost_usd=None)
    before = raw(rig.base.ledger)
    request = rig.prepare()
    receipt = rig.amend(request)
    after = raw(rig.base.ledger)
    assert after["version"] == 2
    for name in ("run", "call"):
        assert after["tables"][name] == before["tables"][name]
    assert after["tables"]["event"][:-1] == before["tables"]["event"]
    snapshot = rig.current.snapshot()
    assert snapshot["started_at"] == START.isoformat()
    assert snapshot["active_workers"] == 2
    assert snapshot["reserved_usd"] == "30.000000"
    assert snapshot["deadline"] is snapshot["budget_usd"] is snapshot["available_usd"] is None
    assert snapshot["initial_configuration"]["budget_micro"] == 100_000_000
    assert receipt["before_ledger_digest"] == request["expected_ledger_digest"]


@pytest.mark.parametrize(
    "variant", ["none", "unbound", "wrong_kind", "wrong_run", "changed", "missing", "symlink"]
)
def test_lp03_41_authority_is_exact_and_scoped(rig, variant):
    request = rig.prepare()
    binding = rig.bindings[rig.user]
    if variant == "unbound":
        rig.bindings.pop(rig.user)
    elif variant in {"wrong_kind", "wrong_run"}:
        binding["kind" if variant == "wrong_kind" else "run_id"] = "foreign"
    elif variant == "changed":
        Path(binding["path"]).write_text('{"reviewed": true}')
    elif variant == "missing":
        Path(binding["path"]).unlink()
    elif variant == "symlink":
        path = Path(binding["path"])
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    control = rig.base if variant == "none" else rig.control()
    before = raw(rig.base.ledger)
    with pytest.raises(ControlError):
        control.amend_policy(**request)
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("expected_run_id", "foreign", "run_conflict"),
        ("expected_policy_revision", 7, "stale_policy"),
        ("expected_policy_digest", "0" * 64, "stale_policy"),
        ("expected_ledger_digest", "0" * 64, "stale_preparation"),
        ("model_matrix_ref", "model_matrix:" + "0" * 64, "authorization_required"),
    ],
)
def test_lp04_wrong_bindings_refuse(rig, field, value, code):
    request = rig.prepare()
    request[field] = value
    before = raw(rig.base.ledger)
    refused(code, rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize("variant", ["reserve", "cap", "settle"])
def test_lp05_intervening_state_is_not_lost(rig, variant):
    if variant == "settle":
        rig.base.reserve("old", label="old", paid=True, max_cost_usd="1")
        rig.base.mark_started("old", worker_identity="old")
        rig.base.record_outcome(
            "old", outcome="failed", process_stopped=True, evidence_ref="observed"
        )
    request = rig.prepare()
    if variant == "reserve":
        rig.base.reserve("new", label="new", paid=False, max_cost_usd=None)
    elif variant == "cap":
        rig.base.set_worker_cap(11, evidence_ref="measured")
    else:
        rig.base.settle_cost("old", actual_cost_usd="1", evidence_ref="actual usage")
    before = raw(rig.base.ledger)
    refused("stale_preparation", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


def test_lp06_replay_is_historical_after_later_work(rig):
    request = rig.prepare()
    receipt = rig.amend(request)
    rig.batch()
    rig.reserve()
    before = raw(rig.base.ledger)
    assert rig.control().amend_policy(**request) == receipt
    assert raw(rig.base.ledger) == before
    assert len(rig.current.snapshot()["calls"]) == 1


@pytest.mark.parametrize(
    "field",
    ["authorization_ref", "model_matrix_ref", "preparation_ref", "expected_policy_revision"],
)
def test_lp07_conflicting_replay_refuses(rig, field):
    request = rig.prepare()
    rig.amend(request)
    request[field] = 2 if field == "expected_policy_revision" else request[field] + "-changed"
    before = raw(rig.base.ledger)
    refused("amendment_conflict", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


def test_lp08_old_authorization_cannot_create_another_policy(rig):
    rig.amend()
    request = rig.prepare()
    request["amendment_id"] = "amend-2"
    before = raw(rig.base.ledger)
    refused("stale_authorization", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize(
    "value", [1000000000, "Infinity", False, {}, {"max_elapsed_seconds": None}]
)
def test_lp09_no_fake_unlimited_record(rig, value):
    data = json.loads(Path(rig.bindings[rig.user]["path"]).read_text())
    data["effective_total_limits"] = value
    rig.user = rig.pin("user_authorization", data)
    request = rig.prepare()
    before = raw(rig.base.ledger)
    refused("invalid_authorization", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize("variant", ["future", "incomplete", "bad_hash", "bad_format"])
def test_lp13_unknown_or_corrupt_schema_refuses(rig, variant):
    if variant in {"bad_hash", "bad_format"}:
        rig.amend()
    with sqlite3.connect(rig.base.ledger) as connection:
        if variant == "future":
            connection.execute("PRAGMA user_version=99")
        elif variant == "incomplete":
            connection.execute("PRAGMA user_version=2")
        elif variant == "bad_hash":
            connection.execute(
                "UPDATE policy_revision SET policy_digest=? WHERE revision=1", ("0" * 64,)
            )
        else:
            row = connection.execute("SELECT data FROM policy_revision WHERE revision=1").fetchone()
            data = json.loads(row[0])
            data["format"] = 99
            connection.execute(
                "UPDATE policy_revision SET data=?,policy_digest=? WHERE revision=1",
                (json.dumps(data), digest(data)),
            )
    before = raw(rig.base.ledger)
    refused("invalid_ledger", rig.control().snapshot)
    assert raw(rig.base.ledger) == before


def test_lp15_unknown_and_active_capacity_retained(rig):
    rig.base.set_worker_cap(1, evidence_ref="measured one slot")
    rig.base.reserve("unknown", label="unknown", paid=True, max_cost_usd="2")
    rig.base.mark_started("unknown", worker_identity="actual-native-observation")
    rig.base.record_outcome(
        "unknown", outcome="unknown", process_stopped=False, evidence_ref="lost observation"
    )
    rig.amend()
    rig.batch()
    refused("capacity_exceeded", rig.reserve)
    assert rig.current.snapshot()["active_workers"] == 1
    assert rig.current.snapshot()["reserved_usd"] == "2.000000"


def test_lp18_stale_or_unbound_new_writer_cannot_mutate(rig):
    rig.amend()
    rig.batch()
    for expected in (None, 0, 2):
        refused(
            "stale_policy",
            rig.control(expected).reserve,
            "stale",
            label="no",
            paid=False,
            max_cost_usd=None,
            batch_id="batch",
            timeout_seconds=1,
        )
    assert rig.control().snapshot()["policy"]["revision"] == 1


def test_lp19_20_after_old_deadline_and_budget(rig):
    rig.amend()
    rig.batch(budget="1000")
    rig.now = START + timedelta(days=4)
    for number in range(3):
        name = str(number)
        rig.reserve(name, cost="60")
        rig.current.mark_started(name, worker_identity=name)
        rig.current.record_outcome(
            name,
            outcome="succeeded",
            process_stopped=True,
            actual_cost_usd="60",
            evidence_ref="authored cost",
        )
    snapshot = rig.current.snapshot()
    assert snapshot["settled_usd"] == "180.000000"
    assert snapshot["elapsed_seconds"] == 4 * 86400
    assert snapshot["available_usd"] is None and not snapshot["budget_breached"]


def test_lp22_unknown_price_and_held_cost_remain_distinct(rig):
    rig.amend()
    rig.batch()
    refused(
        "unknown_cost",
        rig.current.reserve,
        "unknown",
        label="bad",
        paid=True,
        max_cost_usd=None,
        batch_id="batch",
        timeout_seconds=1,
    )
    rig.reserve("held", cost="10")
    rig.current.mark_started("held", worker_identity="held")
    rig.current.record_outcome(
        "held", outcome="failed", process_stopped=True, evidence_ref="observed stop"
    )
    rig.reserve("later", cost="20")
    assert rig.current.snapshot()["reserved_usd"] == "30.000000"
    assert rig.current.snapshot()["calls"][0]["actual_usd"] is None


def test_lp23_actual_overcharge_still_fences_admission(rig):
    rig.amend()
    rig.batch()
    rig.reserve(cost="1")
    rig.current.mark_started("call", worker_identity="observed")
    rig.current.record_outcome(
        "call",
        outcome="failed",
        process_stopped=True,
        actual_cost_usd="200",
        evidence_ref="authored overcharge",
    )
    refused("budget_integrity_failure", rig.reserve, "later")
    assert rig.current.snapshot()["settled_usd"] == "200.000000"


def test_lp32_33_legacy_calls_need_bounds_only_before_launch(rig):
    rig.base.reserve("unstarted", label="old", paid=False, max_cost_usd=None)
    rig.base.reserve("active", label="old", paid=True, max_cost_usd="3")
    rig.base.mark_started("active", worker_identity="observed-native")
    rig.amend()
    rig.batch()
    refused(
        "invocation_bound_required", rig.current.claim_launch, "unstarted", worker_identity="new"
    )
    request = {
        "call_id": "unstarted",
        "batch_id": "batch",
        "timeout_seconds": 2.0,
        "policy_revision": 1,
    }
    record = rig.pin("invocation_binding", {"run_id": "policy-fixture", "binding": request})
    rig.current = rig.control(1)
    bound = rig.current.bind_legacy_invocation(
        expected_revision=1,
        call_id="unstarted",
        batch_id="batch",
        timeout_seconds=2,
        evidence_ref=record,
    )
    assert bound["execution_deadline"] is None
    rig.current.claim_launch("unstarted", worker_identity="new")
    rig.current.record_outcome(
        "active",
        outcome="failed",
        process_stopped=True,
        actual_cost_usd="1",
        evidence_ref="confirmed owned stop",
    )
    snapshot = rig.current.snapshot()
    assert snapshot["active_workers"] == 1
    active = next(c for c in snapshot["calls"] if c["call_id"] == "active")
    assert active["invocation_bound"] is None and active["actual_usd"] == "1.000000"


def test_lp08_18_new_authorization_does_not_allow_stale_writers(rig):
    first_request = rig.prepare()
    first = rig.amend(first_request)
    rig.batch()
    old_control = rig.current
    authority = json.loads(Path(rig.bindings[rig.user]["path"]).read_text())
    authority["id"] = "fixture-auth-new"
    authority["recorded_at_utc"] = (START + timedelta(minutes=2)).isoformat()
    rig.user = rig.pin("user_authorization", authority)
    request = rig.prepare()
    request["amendment_id"] = "amend-2"
    rig.amend(request)
    refused("stale_policy", old_control.set_worker_cap, 11, evidence_ref="stale observation")
    refused(
        "stale_policy",
        rig.current.reserve,
        "old-batch",
        label="old batch",
        paid=False,
        max_cost_usd=None,
        batch_id="batch",
        timeout_seconds=1,
    )
    assert rig.control().amend_policy(**first_request) == first
    assert rig.current.snapshot()["policy"]["revision"] == 2
    authority["id"] = "fixture-auth-unused-old"
    authority["recorded_at_utc"] = (START + timedelta(seconds=30)).isoformat()
    rig.user = rig.pin("user_authorization", authority)
    request = rig.prepare()
    request["amendment_id"] = "amend-3"
    before = raw(rig.base.ledger)
    refused("stale_authorization", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize("bindings", [None, {"untrusted": True}, {"ref": {"reviewed": True}}])
def test_lp03_41_malformed_authority_configuration_refuses(bindings):
    refused("invalid_authorization", PinnedRecords, bindings)


def test_lp04_nonfinite_operation_is_a_typed_refusal(rig):
    request = rig.prepare()
    request["expected_policy_revision"] = float("nan")
    before = raw(rig.base.ledger)
    refused("invalid_input", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before
