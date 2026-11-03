from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from ai_task_manager.application.core import Core
from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work.errors import (
    CapacityExceeded,
    StaleVersion,
    ValidationError,
    WorkError,
)
from ai_task_manager.domains.work.models import Attempt, CapacityPolicy
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import events
from tests.domains.helpers import Rig


def session_facts(rig, session_id):
    with rig.db.transaction() as conn:
        return {
            table: [
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} WHERE session_id=? ORDER BY rowid", (session_id,)
                )
            ]
            for table in ("tasks", "attempts", "events", "outbox", "operations")
        }


def test_p08_two_connections_cannot_reserve_the_same_task(tmp_path):
    rig = Rig(tmp_path / "fixture.sqlite")
    task = rig.queued()
    cores = [rig.new_core(), rig.new_core()]
    barrier = Barrier(2)
    decision = rig.decision(task, S.IN_PROGRESS)
    keys = [rig.key(), rig.key()]

    def reserve(index):
        barrier.wait()
        try:
            return cores[index].execution.reserve(
                rig.handle,
                task.id,
                "host",
                task.version,
                task.decision_revision,
                decision,
                keys[index],
            )
        except WorkError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(not isinstance(result, WorkError) for result in results) == 1
    assert any(isinstance(result, StaleVersion) for result in results)
    with rig.db.transaction() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM attempts WHERE active=1").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("boundary", ["host", "project"])
def test_p08_racing_distinct_tasks_obey_global_and_project_capacity(tmp_path, boundary):
    rig = Rig(
        tmp_path / "fixture.sqlite",
        host_limit=1 if boundary == "host" else 2,
        project_limit=2 if boundary == "host" else 1,
    )
    first = rig.queued()
    # Each contender must lead its own queue before capacity can decide the race.
    scope = replace(
        rig.scope,
        logical_session="independent-capacity-queue",
        project="other-project" if boundary == "host" else rig.scope.project,
    )
    handle = rig.session(scope)
    second = rig.task(handle=handle, confirmed=False)
    second = rig.confirm(second, handle=handle, scope=scope)
    second = rig.core.transitions.transition(
        handle,
        second.id,
        S.TODO,
        "enqueue",
        second.version,
        second.decision_revision,
        rig.decision(second, S.TODO, scope=scope),
        {},
        rig.key(),
    )
    second = rig.confirm(second, handle=handle, scope=scope)
    tasks = [first, second]
    handles, scopes = [rig.handle, handle], [rig.scope, scope]
    assert first.session_id != second.session_id
    before = [session_facts(rig, task.session_id) for task in tasks]
    cores, barrier = [rig.new_core(), rig.new_core()], Barrier(2)
    refs = [
        rig.decision(task, S.IN_PROGRESS, scope=scope)
        for task, scope in zip(tasks, scopes, strict=True)
    ]
    keys = [rig.key(), rig.key()]

    def reserve(index):
        task = tasks[index]
        barrier.wait(timeout=5)
        try:
            return cores[index].execution.reserve(
                handles[index],
                task.id,
                "host",
                task.version,
                task.decision_revision,
                refs[index],
                keys[index],
            )
        except CapacityExceeded as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(isinstance(result, Attempt) for result in results) == 1
    assert sum(isinstance(result, CapacityExceeded) for result in results) == 1
    winner = next(index for index, result in enumerate(results) if isinstance(result, Attempt))
    loser = 1 - winner
    assert results[winner].task_id == tasks[winner].id
    assert session_facts(rig, tasks[loser].session_id) == before[loser]
    with rig.db.transaction() as conn:
        assert [tuple(row) for row in conn.execute("SELECT id,task_id,active FROM attempts")] == [
            (results[winner].id, tasks[winner].id, 1)
        ]
        assert [
            tuple(row) for row in conn.execute("SELECT attempt_id,task_id FROM scheduling_attempts")
        ] == [(results[winner].id, tasks[winner].id)]
        assert conn.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 0
        assert (
            conn.execute("SELECT 1 FROM operations WHERE key=?", (keys[loser],)).fetchone() is None
        )
        assert conn.execute("SELECT 1 FROM operations WHERE key=?", (keys[winner],)).fetchone()


def test_p08_same_session_fifo_precedes_reservation_with_free_capacity(tmp_path):
    rig = Rig(tmp_path / "fixture.sqlite", host_limit=2, project_limit=2)
    first, second = rig.queued(), rig.queued()
    before = session_facts(rig, first.session_id)
    key = rig.key()
    with pytest.raises(SchedulingBlocked, match="An earlier eligible queue item has priority"):
        rig.core.execution.reserve(
            rig.handle,
            second.id,
            "host",
            second.version,
            second.decision_revision,
            rig.decision(second, S.IN_PROGRESS),
            key,
        )
    assert session_facts(rig, first.session_id) == before
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduling_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 0
    for task in (first, second):
        attempt = rig.core.execution.reserve(
            rig.handle,
            task.id,
            "host",
            task.version,
            task.decision_revision,
            rig.decision(task, S.IN_PROGRESS),
            key if task == second else rig.key(),
        )
        assert attempt.task_id == task.id
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts WHERE active=1").fetchone()[0] == 2


def test_p08_project_capacity_covers_multiple_hosts(tmp_path):
    rig = Rig(tmp_path / "fixture.sqlite", project_limit=1)
    Core(rig.db, rig.policy, (CapacityPolicy("host-b", 2, 1),))
    rig.reserved()
    task = rig.queued()
    with pytest.raises(CapacityExceeded):
        rig.core.execution.reserve(
            rig.handle,
            task.id,
            "host-b",
            task.version,
            task.decision_revision,
            rig.decision(task, S.IN_PROGRESS),
            rig.key(),
        )


def test_p25_callers_cannot_raise_persisted_limits_or_pass_limits_to_reserve(tmp_path):
    rig = Rig(tmp_path / "fixture.sqlite", host_limit=1)
    with pytest.raises(ValidationError):
        Core(rig.db, rig.policy, (CapacityPolicy("host", 999, 999),))
    task = rig.queued()
    with pytest.raises(TypeError):
        rig.core.execution.reserve(
            rig.handle,
            task.id,
            "host",
            task.version,
            task.decision_revision,
            rig.decision(task, S.IN_PROGRESS),
            rig.key(),
            limit=999,
        )


def test_p16_transaction_failure_rolls_back_task_event_outbox_notification_and_key(
    tmp_path, monkeypatch
):
    rig = Rig(tmp_path / "fixture.sqlite")
    task = rig.task()
    original_append = events.append
    with rig.db.transaction() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "events", "outbox", "receipts", "operations")
        }

    def fail_after_audit(*args, **kwargs):
        original_append(*args, **kwargs)
        raise RuntimeError("injected failure after event and notification")

    monkeypatch.setattr(events, "append", fail_after_audit)
    with pytest.raises(RuntimeError):
        rig.transition(task, S.TODO, "enqueue", confirm=False)
    assert rig.current(task) == task
    with rig.db.transaction() as conn:
        assert counts == {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts
        }


def test_p09_stale_versions_leave_no_partial_facts(tmp_path):
    rig = Rig(tmp_path / "fixture.sqlite")
    task = rig.task()
    queued = rig.transition(task, S.TODO, "enqueue", confirm=False)
    before = rig.core.notifications.list_events(rig.handle)
    with pytest.raises(StaleVersion):
        rig.transition(task, S.TODO, "enqueue", confirm=False)
    assert rig.current(task) == queued
    assert rig.core.notifications.list_events(rig.handle) == before
