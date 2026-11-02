import pytest

from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition, StaleVersion
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import events
from tests.domains.helpers import Rig


def observation(rig, task, status="Backlog", revision="2026-11-01T00:00:01.000000Z", **kwargs):
    return rig.proof(
        "external_observation",
        task.id,
        {
            "issue_id": task.issue_id,
            "source_revision": revision,
            "status": status,
            "title": task.title,
            "body": task.body,
            "actor_kind": "human",
            "actor_authorized": True,
            "assigned_to_owner": True,
            "reason": "Explicit human request",
            **kwargs,
        },
    )


def test_l21_atomic_supersession_retains_uncertain_operation_and_fence(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task()
    task = rig.transition(task, S.TODO, "enqueue", confirm=False)
    operation = rig.core.sync.claim(rig.handle, "adapter")
    rig.core.sync.record_outcome(
        rig.handle, operation.id, "adapter", operation.attempt_count, "uncertain", "lost response"
    )
    result = rig.core.external.observe(
        rig.handle, task.id, task.version, task.decision_revision, observation(rig, task), rig.key()
    )
    task = rig.current(task)
    old = rig.core.sync.list_operations(rig.handle)[-1]
    assert old.state == "uncertain" and old.fenced_by == result["observation_id"]
    assert task.human_hold and task.observed_status == S.BACKLOG and task.desired_status == S.TODO
    task = rig.core.external.settle(
        rig.handle,
        task.id,
        result["observation_id"],
        task.version,
        task.decision_revision,
        "dequeue",
        {},
        rig.key(),
    )
    assert task.observed_status == S.BACKLOG and task.desired_status == S.BACKLOG
    assert rig.core.sync.list_operations(rig.handle)[-1].state == "uncertain"


def test_l21_missing_or_stale_supersession_context_changes_nothing(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task()
    task = rig.transition(task, S.TODO, "enqueue", confirm=False)
    before = rig.core.sync.list_operations(rig.handle)
    ref = observation(rig, task)
    for version, revision, proof in [(0, 0, ref), (task.version, 99, ref), (task.version, 0, None)]:
        with pytest.raises((StaleVersion, EvidenceRequired)):
            rig.core.external.observe(rig.handle, task.id, version, revision, proof, rig.key())
    assert rig.current(task) == task and rig.core.sync.list_operations(rig.handle) == before


def test_l21_transaction_failure_cannot_leave_a_fence_without_observation(tmp_path, monkeypatch):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task()
    task = rig.transition(task, S.TODO, "enqueue", confirm=False)
    original = events.append

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(events, "append", fail)
    with pytest.raises(RuntimeError):
        rig.core.external.observe(
            rig.handle,
            task.id,
            task.version,
            task.decision_revision,
            observation(rig, task),
            rig.key(),
        )
    assert rig.core.external.observations(rig.handle, task.id) == []
    assert rig.core.sync.list_operations(rig.handle)[-1].fenced_by is None
    assert rig.current(task) == task


def test_l18_unknown_remote_state_records_conflict_and_revokes_work(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task, attempt = rig.running()
    result = rig.core.external.observe(
        rig.handle,
        task.id,
        task.version,
        task.decision_revision,
        observation(rig, task, status="Unknown provider state"),
        rig.key(),
    )
    task = rig.current(task)
    assert task.human_hold and task.observed_status == S.IN_PROGRESS
    with pytest.raises(InvalidTransition):
        rig.core.external.settle(
            rig.handle,
            task.id,
            result["observation_id"],
            task.version,
            task.decision_revision,
            "hold",
            {},
            rig.key(),
        )
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "running"


def test_l21_hold_requires_real_quiescence_and_does_not_fabricate_it(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task, attempt = rig.running()
    result = rig.core.external.observe(
        rig.handle,
        task.id,
        task.version,
        task.decision_revision,
        observation(rig, task, status="On Hold"),
        rig.key(),
    )
    task = rig.current(task)
    with pytest.raises(EvidenceRequired):
        rig.core.external.settle(
            rig.handle,
            task.id,
            result["observation_id"],
            task.version,
            task.decision_revision,
            "hold",
            {},
            rig.key(),
        )
    ref = rig.proof("quiescence", task.id, {"attempt_id": attempt.id, "quiescent": True})
    task = rig.core.external.settle(
        rig.handle,
        task.id,
        result["observation_id"],
        task.version,
        task.decision_revision,
        "hold",
        {"quiescence": ref},
        rig.key(),
    )
    assert task.observed_status == S.ON_HOLD and task.human_hold
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "paused"


def test_l25_additive_core_schema_upgrade_preserves_existing_intents(tmp_path):
    from ai_task_manager.storage.database import Database

    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task(confirmed=False)
    with rig.db.transaction() as conn:
        conn.execute("DROP TABLE external_observations")
        conn.execute("ALTER TABLE outbox DROP COLUMN fenced_by")
        conn.execute("UPDATE metadata SET version=1")
    upgraded = Database(rig.db.path)
    with upgraded.transaction() as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    assert rig.current(task) == task
