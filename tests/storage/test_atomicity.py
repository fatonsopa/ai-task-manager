from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ai_task_manager.application.core import Core
from ai_task_manager.domains.work.errors import (
    CapacityExceeded,
    StaleVersion,
    ValidationError,
    WorkError,
)
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import events
from tests.domains.helpers import Rig


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
    tasks = [rig.queued(), rig.queued()]
    cores, barrier = [rig.new_core(), rig.new_core()], Barrier(2)
    refs = [rig.decision(task, S.IN_PROGRESS) for task in tasks]
    keys = [rig.key(), rig.key()]

    def reserve(index):
        task = tasks[index]
        barrier.wait()
        try:
            return cores[index].execution.reserve(
                rig.handle,
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
    assert sum(isinstance(result, CapacityExceeded) for result in results) == 1


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
