"""Disposable schema-2 controls; no original ledger or native account access."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_task_manager.adapters.process.groups import OwnedProcessGroup, supervise
from tests.test_build_control_policy_amendment import ROOT, raw
from tests.test_build_control_policy_amendment import rig as rig
from tools.build_control import ControlError

SCOPE = {
    "attempt_id": "owned-attempt",
    "host": "codex",
    "configuration_digest": "a" * 64,
    "model": "authored-no-model",
    "effort": "high",
    "purpose": "assignment",
}


def running_invocation(rig, *, start=True):
    rig.now = datetime.now(UTC)
    rig.amend()
    rig.batch(seconds=60, budget="0")
    rig.current.reserve(
        "call",
        label="owned local accounting fixture",
        paid=False,
        max_cost_usd=None,
        batch_id="batch",
        timeout_seconds=30,
        metadata={"invocation_binding": SCOPE},
    )
    rig.current.claim_launch("call", worker_identity="owner")
    if start:
        rig.current.mark_started("call", worker_identity="owner")
    return rig.current


@pytest.mark.parametrize(
    "variant", ["reserved", "launching", "succeeded", "failed", "unknown", "wrong_owner"]
)
def test_effect_requires_exact_running_owner(rig, variant):
    if variant == "reserved":
        rig.amend()
        rig.batch()
        rig.reserve()
    else:
        running_invocation(rig, start=variant != "launching")
        if variant in {"succeeded", "failed", "unknown"}:
            rig.current.record_outcome(
                "call",
                outcome=variant,
                process_stopped=variant != "unknown",
                evidence_ref="authored logical fixture; no external process",
            )
    reached = []
    with pytest.raises(ControlError) as caught:
        with rig.current.effect_boundary(
            "call", worker_identity="foreign" if variant == "wrong_owner" else "owner"
        ):
            reached.append(True)
    assert caught.value.code == "call_conflict" and not reached


@pytest.mark.parametrize("variant", ["paused", "stale_revision", "deadline", "missing_bound"])
def test_effect_uses_current_revision_pause_and_bound(rig, variant):
    if variant == "missing_bound":
        rig.base.reserve("call", label="legacy running fixture", paid=False, max_cost_usd=None)
        rig.base.mark_started("call", worker_identity="owner")
        rig.amend()
    else:
        running_invocation(rig)
    control = rig.current
    if variant == "paused":
        rig.admission(True)
        control = rig.current
    elif variant == "stale_revision":
        control = rig.control(0)
    elif variant == "deadline":
        rig.now += timedelta(seconds=31)
    expected = {
        "paused": "admission_paused",
        "stale_revision": "stale_policy",
        "deadline": "deadline_reached",
        "missing_bound": "deadline_reached",
    }[variant]
    with pytest.raises(ControlError) as caught:
        with control.effect_boundary("call", worker_identity="owner"):
            pytest.fail("A refused invocation reached its effect")
    assert caught.value.code == expected


def test_effect_preserves_bound_and_records_after_restart(rig):
    running_invocation(rig)
    before = rig.current.snapshot()["calls"][0]["invocation_bound"]
    rig.now += timedelta(seconds=1)
    with rig.control(1).effect_boundary("call", worker_identity="owner") as state:
        assert state["run_id"] == "policy-fixture" and state["policy_revision"] == 1
        assert state["call"]["invocation_bound"] == before
    rig.now += timedelta(seconds=31)
    with pytest.raises(ControlError):
        with rig.control(1).effect_boundary("call", worker_identity="owner"):
            pytest.fail("Restart extended the original invocation")
    assert rig.current.snapshot()["calls"][0]["invocation_bound"] == before


def test_effect_scope_is_read_only_on_success(rig):
    running_invocation(rig)
    before = raw(rig.base.ledger)
    with rig.current.effect_boundary("call", worker_identity="owner") as state:
        state["call"]["metadata"]["invocation_binding"]["host"] = "foreign"
    assert raw(rig.base.ledger) == before


def test_effect_failure_keeps_owned_call_and_external_effect(rig):
    running_invocation(rig)
    external = rig.root / "actual-bounded-file-effect"
    with pytest.raises(RuntimeError):
        with rig.current.effect_boundary("call", worker_identity="owner"):
            external.write_bytes(b"published")
            raise RuntimeError("authored lost completion observation")
    assert external.read_bytes() == b"published"
    snapshot = rig.current.snapshot()
    assert snapshot["active_workers"] == 1
    assert snapshot["calls"][0]["status"] == "running"
    assert not snapshot["calls"][0]["cost_settled"]
    rig.current.record_outcome(
        "call", outcome="unknown", process_stopped=False, evidence_ref=str(external)
    )
    with pytest.raises(ControlError):
        with rig.current.effect_boundary("call", worker_identity="owner"):
            pytest.fail("Uncertain invocation was admitted again")


@pytest.mark.parametrize("variant", ["pause_first", "effect_first"])
def test_pause_and_effect_serialize(rig, variant):
    running_invocation(rig)
    ready, release, effect = [rig.root / name for name in ("ready", "release", "effect")]
    source = """
import pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from tools.build_control import BuildControl,ControlError
ledger,ready,release,effect=map(pathlib.Path,sys.argv[2:])
try:
    control=BuildControl(ledger,expected_policy_revision=1)
    with control.effect_boundary('call',worker_identity='owner'):
        ready.write_text('entered')
        until=time.monotonic()+2
        while not release.exists():
            if time.monotonic()>=until: raise RuntimeError('fixture barrier expired')
            time.sleep(.005)
        effect.write_bytes(b'owned')
    print('published')
except ControlError as error:
    print(error.code)
"""
    if variant == "pause_first":
        rig.admission(True)
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            source,
            str(ROOT),
            str(rig.base.ledger),
            str(ready),
            str(release),
            str(effect),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    group = OwnedProcessGroup(child)
    completion = None
    try:
        assert group.capture().known
        if variant == "effect_first":
            until = time.monotonic() + 2
            while not ready.exists():
                assert time.monotonic() < until
                time.sleep(0.005)
            with ThreadPoolExecutor(max_workers=1) as pool:
                paused = pool.submit(rig.admission, True)
                # Bounded fixture interleaving, outside this process's Root lock.
                time.sleep(0.05)
                assert not paused.done()
                release.write_text("continue")
                paused.result(timeout=4)
        completion = supervise(group, timeout_seconds=5)
        stdout, stderr = child.communicate(timeout=1)
        assert completion["group"]["confirmed_stop"] and child.returncode == 0, stderr
        if variant == "pause_first":
            assert stdout.strip() == "admission_paused" and not effect.exists()
        else:
            assert stdout.strip() == "published" and effect.read_bytes() == b"owned"
            with pytest.raises(ControlError):
                with rig.current.effect_boundary("call", worker_identity="owner"):
                    pytest.fail("Effect after committed pause was admitted")
    finally:
        release.touch(exist_ok=True)
        if completion is not None and completion["group"]["confirmed_stop"]:
            # supervise already confirmed stop and reaped its owned leader.
            # Retain that receipt rather than querying a deliberately lost anchor.
            stop_record = completion["group"]
        else:
            stopped = group.stop()
            stop_record = group.evidence(stopped, orphaned=False)
            if stopped.confirmed:
                group.reap_exited_leader()
                child.communicate(timeout=1)
        if evidence := os.environ.get("AITM_TEST_EVIDENCE_DIR"):
            (Path(evidence) / f"build-effect-groups-{variant}.json").write_text(
                json.dumps({"group": stop_record}) + "\n"
            )
        assert stop_record["confirmed_stop"]


def test_launch_boundary_contract_remains_unchanged(rig):
    running_invocation(rig, start=False)
    with rig.current.launch_boundary("call", worker_identity="owner") as started:
        started(process_pid=None, process_start_identity=None)
    before = rig.current.snapshot()
    with rig.current.effect_boundary("call", worker_identity="owner"):
        pass
    assert rig.current.snapshot() == before
    assert len([event for event in before["events"] if event["kind"] == "call_started"]) == 1
