import concurrent.futures
import threading
import time
from dataclasses import replace

import pytest

from ai_task_manager.domains.work.errors import CapacityExceeded, InvalidTransition
from ai_task_manager.domains.work.models import AttemptState
from ai_task_manager.supervisor.accounting import MechanicalCommands
from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeUncertain


def test_waiting_harness_cannot_perform_assigned_work(rr):
    dispatch = rr.start()
    attempt = rr.rig.core.get_attempt(rr.rig.handle, dispatch.attempt_id)
    assert attempt.state == AttemptState.STARTING
    assert not (dispatch.resources.worktree / "ran").exists()
    fact = rr.journal.observation(dispatch.worker.observation_id)
    assert fact["runtime_stage"] == "waiting_harness_initialized"
    assert fact["model_execution"] == "not_started"
    command = rr.command(dispatch, "from pathlib import Path; Path('ran').touch()")
    with pytest.raises(InvalidTransition):
        rr.supervisor.execute(rr.rig.handle, dispatch, command, operation_key=rr.rig.key())
    assert not (dispatch.resources.worktree / "ran").exists()


def test_confirmed_composition_runs_real_fixture_and_keeps_delivery_separate(rr):
    dispatch = rr.start()
    attempt = rr.admit(dispatch)
    assert attempt.state == AttemptState.RUNNING
    result = rr.supervisor.execute(
        rr.rig.handle, dispatch, rr.command(dispatch), operation_key=rr.rig.key()
    )
    assert result.outcome == "done" and result.quiescent
    assert "fixture complete" in dispatch.worker.stdout.read_text()
    task = rr.rig.core.get_task(rr.rig.handle, attempt.task_id)
    assert task.observed_status == "In Progress"
    assert task.completed_work == ()
    assert rr.journal.observation(result.observation_id)["model_success"] == "not_established"


def test_command_can_commit_real_disposable_fixture_change(rr):
    dispatch = rr.start()
    rr.admit(dispatch)
    program = """from pathlib import Path
import subprocess
Path('result.txt').write_text('verified fixture')
subprocess.run(['git','add','result.txt'],check=True)
subprocess.run(['git','commit','-qm','Fixture work'],check=True)
"""
    result = rr.supervisor.execute(
        rr.rig.handle, dispatch, rr.command(dispatch, program), operation_key=rr.rig.key()
    )
    assert result.outcome == "done"
    assert (dispatch.resources.worktree / "result.txt").read_text() == "verified fixture"


def test_missing_provider_confirmation_never_grants_work(rr):
    dispatch = rr.start()
    rr.supervisor.provider.confirm_pending = lambda *args: None
    with pytest.raises(InvalidTransition):
        rr.admit(dispatch)
    assert rr.rig.core.get_attempt(rr.rig.handle, dispatch.attempt_id).state == "starting"
    assert rr.journal.effect(dispatch.attempt_id, "work_grant") is None


def test_resource_replacement_refuses_admission(rr):
    dispatch = rr.start()
    dispatch.resources.database.rename(dispatch.resources.database.with_suffix(".old"))
    dispatch.resources.database.touch()
    with pytest.raises(RuntimeRefused):
        rr.admit(dispatch)


def test_human_hold_prevents_next_command(rr):
    dispatch = rr.start()
    rr.admit(dispatch)
    rr.hold(dispatch)
    with pytest.raises(InvalidTransition):
        rr.supervisor.execute(
            rr.rig.handle,
            dispatch,
            rr.command(dispatch, "raise Exception('never')"),
            operation_key=rr.rig.key(),
        )
    attempt = rr.rig.core.get_attempt(rr.rig.handle, dispatch.attempt_id)
    assert attempt.state == "failed"
    assert dispatch.worker.process.returncode is not None


def test_hold_during_work_stops_owned_process(rr):
    dispatch = rr.start()
    rr.admit(dispatch)
    marker = dispatch.resources.worktree / "work-started"
    command = rr.command(
        dispatch,
        "from pathlib import Path;import time;Path('work-started').touch();time.sleep(10)",
        timeout=20,
    )
    errors = []

    def hold():
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not marker.exists():
            errors.append("Actual command startup was not observed")
            return
        rr.hold(dispatch)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        result = rr.supervisor.execute(rr.rig.handle, dispatch, command, operation_key=rr.rig.key())
    finally:
        thread.join()
    assert not errors
    assert result.outcome == "failed" and result.quiescent
    assert "authority_boundary" in result.reason


@pytest.mark.parametrize("mutation", ["deadline", "accounting", "cwd", "stale_attempt", "session"])
def test_guarded_command_boundary_refuses_stale_authority(rr, mutation):
    dispatch = rr.start()
    attempt = rr.admit(dispatch)
    command = rr.command(dispatch)
    version = attempt.version
    if mutation == "deadline":
        rr.rig.now += 121
    elif mutation == "accounting":
        rr.supervisor.accounting = MechanicalCommands()
    elif mutation == "cwd":
        command = replace(command, cwd=rr.repo)
    elif mutation == "stale_attempt":
        version -= 1
    else:
        rr.rig.core.revoke_session(rr.rig.handle)
    with pytest.raises(Exception):
        rr.supervisor.check_boundary(rr.rig.handle, dispatch, command, version)
    assert rr.journal.effect(dispatch.attempt_id, "work_grant") is None


def test_failed_and_timed_out_commands_retain_observed_failure(rr):
    first = rr.start()
    rr.admit(first)
    failed = rr.supervisor.execute(
        rr.rig.handle, first, rr.command(first, "raise SystemExit(7)"), operation_key=rr.rig.key()
    )
    assert failed.outcome == "failed" and failed.quiescent
    fact = rr.journal.observation(failed.observation_id)
    assert fact["completion"]["returncode"] == 7
    second = rr.start()
    rr.admit(second)
    timed = rr.supervisor.execute(
        rr.rig.handle,
        second,
        rr.command(second, "import time; time.sleep(10)", timeout=0.08),
        operation_key=rr.rig.key(),
    )
    assert timed.outcome == "failed" and timed.reason == "timeout"
    assert rr.journal.observation(failed.observation_id) == fact


def test_duplicate_launch_claim_never_creates_second_process(rr):
    dispatch = rr.start()
    with pytest.raises(RuntimeUncertain):
        rr.runtime.launch_waiting(dispatch.attempt_id)
    assert len(rr.journal.observations(dispatch.attempt_id, "process_created")) == 1


def test_concurrent_starts_claim_one_task_before_effects(rr):
    r = rr.rig
    task = r.queued()
    decision = r.decision(task, "In Progress")
    barrier = threading.Barrier(2)

    def launch(n):
        barrier.wait()
        try:
            return rr.supervisor.start(
                r.handle,
                task.id,
                "fixture",
                expected_version=task.version,
                decision_revision=task.decision_revision,
                decision_ref=decision,
                operation_key=f"parallel:{n}",
                deadline=r.now + 30,
            )
        except Exception as error:
            return error

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(launch, range(2)))
    started = [result for result in results if not isinstance(result, Exception)]
    assert len(started) == 1
    assert len(rr.runtime.workers) == 1
    assert len(list((rr.path / "owned/trees").iterdir())) == 1


def test_full_capacity_produces_no_new_runtime_effects(rr):
    rr.start()
    rr.start()
    with pytest.raises(CapacityExceeded):
        rr.start()
    assert len(rr.runtime.workers) == 2
    assert len(list((rr.path / "owned/trees").iterdir())) == 2


def test_checkpoint_does_not_finish_work_or_change_model_evidence(rr):
    dispatch = rr.start()
    attempt = rr.admit(dispatch)
    rr.rig.core.execution.checkpoint(
        rr.rig.handle, attempt.id, attempt.version, "heartbeat", {"claim": "done"}, rr.rig.key()
    )
    task = rr.rig.core.get_task(rr.rig.handle, attempt.task_id)
    assert task.completed_work == () and task.execution_state == "running"
    assert rr.journal.effect(attempt.id, "work_grant") is None


def test_resources_and_startup_cannot_be_omitted(rr):
    task, attempt = rr.rig.reserved()
    with pytest.raises(Exception):
        rr.rig.core.execution.running(
            rr.rig.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            "runtime-proof:missing",
            rr.rig.key(),
        )


def test_reassignment_revision_invalidates_current_lease(rr):
    dispatch = rr.start()
    attempt = rr.admit(dispatch)
    task = rr.rig.core.get_task(rr.rig.handle, attempt.task_id)
    ref = rr.rig.proof(
        "human_decision",
        task.id,
        {
            "previous_revision": task.decision_revision,
            "hold": False,
            "assignment": "other-principal",
        },
    )
    rr.rig.core.decisions.record(rr.rig.handle, task.id, task.version, ref, rr.rig.key())
    with pytest.raises(InvalidTransition):
        rr.supervisor.check_boundary(rr.rig.handle, dispatch, rr.command(dispatch), attempt.version)
