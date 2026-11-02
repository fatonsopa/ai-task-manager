"""Finite invocation/continuation controls under explicitly uncapped totals."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_build_control_policy_amendment import ROOT, raw, refused
from tests.test_build_control_policy_amendment import rig as rig
from tools.build_control import ControlError, run_command


def test_lp24_registration_replay_and_conflict(rig):
    rig.amend()
    receipt, request = rig.batch()
    before = raw(rig.base.ledger)
    assert rig.current.register_batch(**request) == receipt
    refused("batch_conflict", rig.current.register_batch, **(request | {"max_invocations": 21}))
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_invocations", 0),
        ("max_invocations", True),
        ("max_invocations", 1.5),
        ("max_invocation_seconds", 0),
        ("max_invocation_seconds", float("inf")),
        ("max_invocation_seconds", float("nan")),
        ("max_incremental_spend_usd", "-1"),
        ("review_ref", "not-a-real-review"),
        ("expected_revision", 0),
    ],
)
def test_lp25_invalid_or_unreviewed_batches_refuse(rig, field, value):
    rig.amend()
    _, request = rig.batch()
    request = request | {"batch_id": "invalid", field: value}
    before = raw(rig.base.ledger)
    with pytest.raises(ControlError):
        rig.current.register_batch(**request)
    assert raw(rig.base.ledger) == before


def test_lp26_invocation_and_cost_allowances_are_atomic(rig):
    rig.amend()
    rig.batch(count=1, budget="1")
    first = rig.reserve(cost="1")
    assert rig.reserve(cost="1") == first
    refused("batch_exhausted", rig.reserve, "extra", cost="1")
    rig.batch("cost", count=3, budget="2")
    rig.reserve("one", cost="2", batch="cost")
    refused("batch_budget_exceeded", rig.reserve, "two", cost="1", batch="cost")


def test_lp21_concurrent_connections_cannot_overclaim(rig):
    rig.amend()
    rig.batch(count=4, budget="3")
    rig.current.set_worker_cap(2, evidence_ref="measured fixture cap")

    def reserve(number):
        try:
            rig.control(1).reserve(
                str(number),
                label="race",
                paid=True,
                max_cost_usd="1",
                batch_id="batch",
                timeout_seconds=1,
            )
            return "reserved"
        except ControlError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(10)))
    assert results.count("reserved") == 2
    assert results.count("capacity_exceeded") == 8
    assert rig.current.snapshot()["reserved_usd"] == "2.000000"
    assert rig.current.snapshot()["active_workers"] == 2


def test_lp27_28_reviewed_continuations_have_no_total_round_cap(rig):
    rig.amend()
    rig.batch("b0", count=1, budget="0")
    for number in range(7):
        current = "b" + str(number)
        rig.reserve("call-" + str(number), batch=current)
        rig.current.mark_started("call-" + str(number), worker_identity="observed")
        rig.current.record_outcome(
            "call-" + str(number),
            outcome="failed",
            process_stopped=True,
            evidence_ref="retained authored failure",
        )
        checkpoint = rig.checkpoint()
        rig.batch("b" + str(number + 1), count=1, budget="0", parent=current, checkpoint=checkpoint)
    assert len(rig.current.snapshot()["batches"]) == 8
    assert all(c["status"] == "failed" for c in rig.current.snapshot()["calls"])


@pytest.mark.parametrize(
    "variant",
    [
        "missing_checkpoint",
        "foreign_checkpoint",
        "stale_checkpoint",
        "changed_review",
        "no_reason",
        "unknown_parent",
    ],
)
def test_lp27_continuation_evidence_is_resolved(rig, variant):
    rig.amend()
    _, first = rig.batch()
    checkpoint = rig.checkpoint()
    if variant == "missing_checkpoint":
        checkpoint = None
    elif variant == "foreign_checkpoint":
        checkpoint = rig.pin("checkpoint", rig.current.snapshot(), run="foreign")
    elif variant == "stale_checkpoint":
        rig.reserve("intervening")
    parent = "unknown" if variant == "unknown_parent" else "batch"
    # Build a valid reviewed control first in a separate fixture; the operation
    # being assessed must not be reached by a setup helper on the target ledger.
    definition = {
        "batch_id": "next",
        "policy_revision": 1,
        "max_invocations": 1,
        "max_invocation_seconds": 1.0,
        "max_incremental_spend_micro": 0,
        "continuation_of": parent,
        "checkpoint_sha256": checkpoint.split(":")[1] if checkpoint else None,
        "reason": "" if variant == "no_reason" else "Reviewed evidence supports continuation",
    }
    review = rig.pin("batch_review", {"run_id": "policy-fixture", "definition": definition})
    if variant == "changed_review":
        Path(rig.bindings[review]["path"]).write_text("{}")
    before = raw(rig.base.ledger)
    with pytest.raises(ControlError):
        rig.control(1).register_batch(
            expected_revision=1,
            batch_id="next",
            max_invocations=1,
            max_invocation_seconds=1,
            max_incremental_spend_usd="0",
            review_ref=review,
            reason=definition["reason"],
            continuation_of=parent,
            checkpoint_ref=checkpoint,
        )
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize(
    "variant", ["missing_batch", "foreign_batch", "missing_timeout", "infinite", "overlong"]
)
def test_lp29_new_invocations_require_actual_bounds(rig, variant):
    rig.amend()
    rig.batch(seconds=2)
    batch = (
        None if variant == "missing_batch" else "foreign" if variant == "foreign_batch" else "batch"
    )
    timeout = (
        None
        if variant == "missing_timeout"
        else float("inf")
        if variant == "infinite"
        else 3
        if variant == "overlong"
        else 1
    )
    before = raw(rig.base.ledger)
    with pytest.raises(ControlError):
        rig.reserve(batch=batch, timeout=timeout)
    assert raw(rig.base.ledger) == before


def test_lp30_deadline_is_assigned_once_and_replay_cannot_extend(rig):
    rig.amend()
    rig.batch()
    rig.reserve()
    claim = rig.current.claim_launch("call", worker_identity="observed")
    deadline = claim["invocation_bound"]["execution_deadline"]
    rig.now += timedelta(seconds=1)
    refused("already_executed", rig.current.claim_launch, "call", worker_identity="observed")
    rig.current.mark_started("call", worker_identity="observed")
    assert (
        rig.current.mark_started("call", worker_identity="observed")["invocation_bound"][
            "execution_deadline"
        ]
        == deadline
    )


def test_lp31_expired_spawn_boundary_retains_uncertainty(rig):
    rig.amend()
    rig.batch()
    rig.reserve(timeout=1)
    rig.current.claim_launch("call", worker_identity="observed")
    rig.now += timedelta(seconds=1)
    with pytest.raises(ControlError) as caught:
        with rig.current.launch_boundary("call", worker_identity="observed"):
            pytest.fail("an expired invocation reached the actual spawn boundary")
    assert caught.value.code == "deadline_reached"
    assert rig.current.snapshot()["active_workers"] == 1
    # Actual observed startup is a fact even when reported late; it does not
    # grant an extended execution deadline or prove supervision/stop.
    late = rig.current.mark_started("call", worker_identity="observed")
    assert late["invocation_bound"]["execution_deadline"] == rig.now.isoformat()
    assert not late["process_stopped"]


@pytest.mark.parametrize(
    "program,outcome", [('print("bounded")', "succeeded"), ("import time; time.sleep(5)", "failed")]
)
def test_lp34_35_actual_process_without_global_deadline(rig, program, outcome):
    rig.amend()
    rig.batch()
    rig.now += timedelta(days=4)
    result = run_command(
        rig.current,
        call_id="child",
        command=[sys.executable, "-c", program],
        paid=False,
        max_cost_usd=None,
        timeout_seconds=0.1,
        batch_id="batch",
        output_directory=rig.root / "traces",
    )
    assert result["call"]["status"] == outcome and result["call"]["process_stopped"]
    assert rig.current.snapshot()["active_workers"] == 0
    if outcome == "failed":
        assert result["call"]["reason"] == "timeout"


def test_lp37_38_39_pause_and_compatible_resume(rig):
    rig.amend()
    rig.batch()
    rig.reserve("running")
    rig.current.mark_started("running", worker_identity="actual")
    rig.reserve("unstarted")
    pause, request = rig.admission(True)
    assert rig.current.pause_admission(**request) == pause
    refused("admission_paused", rig.reserve, "new")
    refused("admission_paused", rig.current.claim_launch, "unstarted", worker_identity="no")
    rig.current.record_outcome(
        "running", outcome="failed", process_stopped=True, evidence_ref="actual stop observation"
    )
    paused = raw(rig.base.ledger)
    rig.admission(False)
    rig.reserve("after")
    current = raw(rig.base.ledger)
    assert current["version"] == 2
    assert current["tables"]["event"][: len(paused["tables"]["event"])] == paused["tables"]["event"]
    assert len(current["tables"]["call"]) == 3


@pytest.mark.parametrize(
    "variant", ["unbound_review", "missing_checkpoint", "stale_revision", "stale_checkpoint"]
)
def test_lp38_resume_requires_current_actual_records(rig, variant):
    rig.amend()
    rig.batch()
    rig.admission(True)
    checkpoint = rig.checkpoint()
    review = rig.pin(
        "admission_review",
        {
            "operation_id": "resume",
            "run_id": "policy-fixture",
            "policy_revision": 1,
            "paused": False,
            "checkpoint_sha256": checkpoint.split(":")[1],
            "reason": "Reviewed reconciliation",
        },
    )
    if variant == "unbound_review":
        rig.bindings.pop(review)
    if variant == "missing_checkpoint":
        checkpoint = None
    if variant == "stale_checkpoint":
        rig.current.set_worker_cap(11, evidence_ref="new measured observation")
    before = raw(rig.base.ledger)
    with pytest.raises(ControlError):
        rig.control(1).resume_admission(
            operation_id="resume",
            expected_revision=0 if variant == "stale_revision" else 1,
            review_ref=review,
            checkpoint_ref=checkpoint,
        )
    assert raw(rig.base.ledger) == before


@pytest.mark.parametrize("variant", ["fable", "one_per_host", "unknown_effort", "duplicate"])
def test_lp46_profile_matrix_refuses_omissions_and_substitution(rig, variant):
    data = json.loads(Path(rig.bindings[rig.matrix]["path"]).read_text())
    if variant == "fable":
        data["selected_profiles"][0]["model"] = "Fable"
    elif variant == "one_per_host":
        data["selected_profiles"] = data["selected_profiles"][::2]
    elif variant == "unknown_effort":
        data["selected_profiles"][0]["reasoning_effort"] = None
    else:
        data["selected_profiles"][1] = dict(data["selected_profiles"][0])
    rig.matrix = rig.pin("model_matrix", data)
    request = rig.prepare()
    before = raw(rig.base.ledger)
    refused("invalid_authorization", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


def test_lp40_actual_cli_from_unrelated_directory(rig, tmp_path):
    request = rig.prepare()
    config = rig.root / "authority.json"
    config.write_text(json.dumps({"format": 1, "bindings": rig.bindings}))
    sha = __import__("hashlib").sha256(config.read_bytes()).hexdigest()
    command = [
        sys.executable,
        str(ROOT / "tools/build_control.py"),
        "--ledger",
        str(rig.base.ledger),
        "--authority-config",
        str(config),
        "--authority-sha256",
        sha,
    ]
    directory = tmp_path / "unrelated"
    directory.mkdir()
    result = subprocess.run(
        command + ["amend-policy", "--request-json", json.dumps(request)],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    result = subprocess.run(
        command + ["status"], cwd=directory, capture_output=True, text=True, timeout=10
    )
    assert (
        result.returncode == 0
        and json.loads(result.stdout)["policy"]["digest"] == receipt["policy_digest"]
    )


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_lp42_backup_cannot_overwrite_ledger_or_sidecars(rig, suffix):
    before = raw(rig.base.ledger)
    refused("invalid_input", rig.base.prepare_policy_amendment, str(rig.base.ledger) + suffix)
    assert raw(rig.base.ledger) == before


def test_lp21_independent_processes_obey_batch_allowance(rig):
    rig.amend()
    rig.batch(count=10, budget="3")
    program = """
import sys
from tools.build_control import BuildControl,ControlError
control=BuildControl(sys.argv[1],expected_policy_revision=1)
try:
 control.reserve(sys.argv[2],label='authored process race',paid=True,max_cost_usd='1',
                 batch_id='batch',timeout_seconds=1)
 print('reserved')
except ControlError as error:
 print(error.code)
"""
    children = []
    try:
        for number in range(6):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", program, str(rig.base.ledger), "process-" + str(number)],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            results.append(stdout.strip())
        assert results.count("reserved") == 3
        assert results.count("batch_budget_exceeded") == 3
        assert rig.current.snapshot()["reserved_usd"] == "3.000000"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)


def test_lp37_actual_spawn_and_pause_are_serialized(rig, monkeypatch):
    import threading

    from tools.build_support import process as execution

    rig.amend()
    rig.batch()
    review = rig.pin(
        "admission_review",
        {
            "operation_id": "pause-race",
            "run_id": "policy-fixture",
            "policy_revision": 1,
            "paused": True,
            "checkpoint_sha256": None,
            "reason": "Authored concurrent pause observation",
        },
    )
    rig.current = rig.control(1)
    entered, release = threading.Event(), threading.Event()
    original = execution.subprocess.Popen
    command = [sys.executable, "-c", "print('owned race fixture')"]
    timeline = []

    def observed_spawn(argv, *args, **kwargs):
        if argv == command:
            entered.set()
            assert release.wait(timeout=5)
            result = original(argv, *args, **kwargs)
            timeline.append("spawn")
            return result
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(execution.subprocess, "Popen", observed_spawn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        run = pool.submit(
            run_command,
            rig.current,
            call_id="spawn-race",
            command=command,
            paid=False,
            max_cost_usd=None,
            timeout_seconds=2,
            batch_id="batch",
            output_directory=rig.root / "race-traces",
        )
        assert entered.wait(timeout=5)

        def pause():
            result = rig.current.pause_admission(
                operation_id="pause-race", expected_revision=1, review_ref=review
            )
            timeline.append("pause-committed")
            return result

        pause_result = pool.submit(pause)
        release.set()
        pause_result.result(timeout=8)
        result = run.result(timeout=8)
    assert result["call"]["process_stopped"]
    assert timeline == ["spawn", "pause-committed"]
    assert rig.current.snapshot()["admission_paused"]


def test_lp37_pause_after_claim_prevents_actual_spawn(rig):
    rig.amend()
    rig.batch()
    rig.reserve()
    rig.current.claim_launch("call", worker_identity="claimed")
    rig.admission(True)
    with pytest.raises(ControlError) as caught:
        with rig.current.launch_boundary("call", worker_identity="claimed"):
            pytest.fail("pause allowed entry to the actual spawn boundary")
    assert caught.value.code == "admission_paused"
    assert rig.current.snapshot()["active_workers"] == 1
