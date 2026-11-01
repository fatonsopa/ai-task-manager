from dataclasses import replace

import pytest

from ai_task_manager.domains.work.errors import (
    CapacityExceeded,
    EvidenceRequired,
    InvalidTransition,
    StaleDecision,
    StaleVersion,
)
from ai_task_manager.domains.work.models import AttemptState as A
from ai_task_manager.domains.work.models import IssueStatus as S

from .helpers import Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "fixture.sqlite")


def test_p13_reserved_starting_and_running_are_distinct_verified_facts(rig):
    task, attempt = rig.reserved()
    assert attempt.state == A.RESERVED and attempt.worker_id is None
    assert task.observed_status == S.TODO and task.execution_state == A.RESERVED
    with pytest.raises(InvalidTransition):
        rig.core.execution.running(
            rig.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            rig.resources(task, attempt),
            rig.key(),
        )
    with pytest.raises(EvidenceRequired):
        rig.core.execution.start(
            rig.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            None,
            rig.decision(task, S.IN_PROGRESS),
            rig.key(),
        )
    start = rig.proof(
        "worker_start",
        attempt.id,
        {"task_id": task.id, "host": "host", "worker_id": "worker-verified"},
    )
    attempt = rig.core.execution.start(
        rig.handle,
        attempt.id,
        task.version,
        task.decision_revision,
        start,
        rig.decision(task, S.IN_PROGRESS),
        rig.key(),
    )
    task = rig.current(task)
    assert attempt.state == A.STARTING and task.observed_status == S.TODO
    with pytest.raises(InvalidTransition):
        rig.core.execution.running(
            rig.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            rig.resources(task, attempt),
            rig.key(),
        )
    task = rig.confirm(task)
    attempt = rig.core.execution.running(
        rig.handle,
        attempt.id,
        task.version,
        task.decision_revision,
        rig.resources(task, attempt),
        rig.key(),
    )
    assert attempt.state == A.RUNNING and rig.current(task).execution_state == A.RUNNING


@pytest.mark.parametrize(
    "override",
    [
        {"ready": False},
        {"ready": 1},
        {"host": "foreign-host"},
        {"task_id": "foreign-task"},
        {"worker_id": "foreign-worker"},
        {"worktree_id": None},
        {"database_id": None},
    ],
)
def test_p24_unready_or_foreign_resources_do_not_grant_work_lease(rig, override):
    task, attempt = rig.starting()
    task = rig.confirm(task)
    count = len(rig.core.notifications.list_events(rig.handle))
    with pytest.raises(EvidenceRequired):
        rig.core.execution.running(
            rig.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            rig.resources(task, attempt, **override),
            rig.key(),
        )
    assert rig.core.get_attempt(rig.handle, attempt.id).state == A.STARTING
    assert len(rig.core.notifications.list_events(rig.handle)) == count


def test_p24_missing_and_wrong_attempt_resource_proof_refused(rig):
    task, attempt = rig.starting()
    task = rig.confirm(task)
    ref = rig.resources(task, attempt)
    rig.proofs.evidence[ref] = replace(
        rig.proofs.evidence[ref], subject="foreign-attempt"
    )
    for candidate in (None, ref):
        with pytest.raises(EvidenceRequired):
            rig.core.execution.running(
                rig.handle,
                attempt.id,
                task.version,
                task.decision_revision,
                candidate,
                rig.key(),
            )


def test_p24_database_cannot_be_shared_by_concurrent_tasks(rig):
    _first, owner = rig.running()
    second, attempt = rig.starting()
    second = rig.confirm(second)
    with pytest.raises(EvidenceRequired):
        rig.core.execution.running(
            rig.handle,
            attempt.id,
            second.version,
            second.decision_revision,
            rig.resources(second, attempt, database_id=owner.database_id),
            rig.key(),
        )
    with rig.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)
            ).fetchone()[0]
            == 0
        )


def test_p05_p09_human_hold_precedes_stale_start_or_automation(rig):
    task, attempt = rig.reserved()
    old = task
    ref = rig.proof(
        "human_decision",
        task.id,
        {"previous_revision": 0, "hold": True, "assignment": "human-owner"},
    )
    task = rig.core.decisions.record(rig.handle, task.id, task.version, ref, rig.key())
    assert task.human_hold and task.decision_revision == 1
    startup = rig.proof(
        "worker_start",
        attempt.id,
        {"task_id": task.id, "host": "host", "worker_id": "worker"},
    )
    with pytest.raises(StaleVersion):
        rig.core.execution.start(
            rig.handle,
            attempt.id,
            old.version,
            old.decision_revision,
            startup,
            rig.decision(old, S.IN_PROGRESS),
            rig.key(),
        )
    with pytest.raises(StaleDecision):
        rig.core.execution.start(
            rig.handle,
            attempt.id,
            task.version,
            0,
            startup,
            rig.decision(task, S.IN_PROGRESS),
            rig.key(),
        )
    with pytest.raises(StaleDecision):
        rig.core.execution.start(
            rig.handle,
            attempt.id,
            task.version,
            1,
            startup,
            rig.decision(task, S.IN_PROGRESS),
            rig.key(),
        )
    assert rig.core.get_attempt(rig.handle, attempt.id).state == A.RESERVED


def test_p05_human_hold_revokes_work_authority_without_fabricating_worker_stop(rig):
    task, attempt = rig.running()
    assert (
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)
        == attempt
    )
    ref = rig.proof("human_decision", task.id, {"previous_revision": 0, "hold": True})
    rig.core.decisions.record(rig.handle, task.id, task.version, ref, rig.key())
    with pytest.raises(InvalidTransition):
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)
    assert rig.core.get_attempt(rig.handle, attempt.id).state == A.RUNNING
    with rig.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT active FROM attempts WHERE id=?", (attempt.id,)
            ).fetchone()[0]
            == 1
        )


def test_p14_unknown_outcome_survives_restart_and_retains_capacity(rig):
    task, attempt = rig.running()
    task, attempt = rig.settle(task, attempt, "unknown", quiescent=False)
    assert attempt.state == A.UNKNOWN and task.observed_status == S.IN_PROGRESS
    rig.core = rig.new_core()
    assert rig.core.get_attempt(rig.handle, attempt.id).state == A.UNKNOWN
    with pytest.raises((CapacityExceeded, InvalidTransition)):
        rig.reserved(task)
    task, attempt = rig.settle(task, attempt, "failed")
    assert attempt.state == A.FAILED
    task, retried = rig.reserved(task)
    assert retried.id != attempt.id
    assert rig.core.get_attempt(rig.handle, attempt.id).state == A.FAILED


def test_p14_unconfirmed_stop_cannot_release_attempt_ownership(rig):
    task, attempt = rig.running()
    with pytest.raises(EvidenceRequired):
        rig.settle(task, attempt, "failed", quiescent=False)
    assert rig.core.get_attempt(rig.handle, attempt.id).state == A.RUNNING


def test_p15_checkpoint_records_report_without_fabricating_success(rig):
    task, attempt = rig.running()
    checkpoint = rig.core.execution.checkpoint(
        rig.handle,
        attempt.id,
        attempt.version,
        "progress",
        {"reported": "done", "tests": "passed"},
        rig.key(),
    )
    assert checkpoint
    assert rig.core.get_attempt(rig.handle, attempt.id) == attempt
    assert rig.current(task) == task and task.completed_work == ()
    with pytest.raises(StaleVersion):
        rig.core.execution.checkpoint(
            rig.handle, attempt.id, 0, "progress", {}, rig.key()
        )


def test_p26_independent_partial_work_oracle_blocks_whole_issue_merge(rig):
    # The required pair is defined by the accepted task, independently of attempts.
    task, attempt = rig.running(rig.queued(units=("probe", "fix")))
    task, attempt = rig.settle(task, attempt, "done")
    assert task.completed_work == ()
    task = rig.complete(task, "probe")
    assert task.completed_work == ("probe",)
    merge = rig.delivery(task)
    with pytest.raises(InvalidTransition):
        rig.transition(task, S.IN_DEV, "merge", {"merge": merge["merge"]})
    assert rig.current(task).observed_status == S.IN_PROGRESS
    task = rig.complete(task, "fix")
    task = rig.transition(task, S.IN_DEV, "merge", {"merge": merge["merge"]})
    assert task.observed_status == S.IN_DEV and task.delivered_once


@pytest.mark.parametrize("delivered", [S.IN_DEV, S.IN_STAGING, S.DONE])
def test_p04_delivered_status_requires_separate_linked_backlog_issue(rig, delivered):
    task, attempt = rig.running()
    task, attempt = rig.settle(task, attempt)
    task = rig.complete(task)
    proof = rig.delivery(task)
    task = rig.transition(task, S.IN_DEV, "merge", {"merge": proof["merge"]})
    if delivered in {S.IN_STAGING, S.DONE}:
        task = rig.transition(
            task,
            S.IN_STAGING,
            "stage",
            {"merge": proof["merge"], "staging": proof["staging"]},
        )
    if delivered == S.DONE:
        task = rig.transition(
            task,
            S.DONE,
            "release",
            {k: proof[k] for k in ("merge", "staging", "production")},
        )
    with pytest.raises(InvalidTransition):
        rig.reserved(task)
    from ai_task_manager.application.intake import prose_digest
    from ai_task_manager.domains.work.models import TaskProse

    prose = TaskProse("Verify follow-up", "Investigate the newly reported case.")
    approval = rig.approval(prose)
    with pytest.raises(EvidenceRequired):
        rig.core.intake.submit(
            rig.handle, prose, approval, rig.key(), parent_task_id=task.id
        )
    authorization = rig.proof(
        "delivered_followup",
        task.id,
        {
            "prose_digest": prose_digest(prose, rig.renderer.render(prose)),
            "target": S.BACKLOG,
        },
    )
    followup = rig.core.intake.submit(
        rig.handle,
        prose,
        approval,
        rig.key(),
        parent_task_id=task.id,
        followup_ref=authorization,
    )
    assert followup.parent_task_id == task.id and followup.desired_status == S.BACKLOG
    assert followup.observed_status is None and followup.id != task.id
