"""Committed admission facts drive bounded fair progress and truthful views."""

from types import SimpleNamespace

import pytest

from ai_task_manager.application.service.projection import task_view
from ai_task_manager.domains.scheduling.models import Dependency, SchedulingBlocked
from ai_task_manager.domains.work.errors import CapacityExceeded, WorkError
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage.database import Database

from .helpers import declare
from .test_guards import reserve


def low(rig):
    task = rig.queued()
    declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now, priority="low")
    return task


def high(rig):
    task = rig.queued()
    declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now, priority="high")
    return task


def admit_and_finish(rig, task):
    task, attempt = rig.running(task)
    return rig.settle(task, attempt)[0]


def test_four_actual_later_admissions_promote_oldest_waiter_and_survive_restart(rig):
    oldest = low(rig)
    later = low(rig)
    for index in range(4):
        winner = high(rig)
        admit_and_finish(rig, winner)
        assert rig.core.scheduling.explain(rig.handle, oldest.id)["bypasses"] == index + 1
    rig.now += 1
    declare(rig.core, rig.handle, oldest, rig.scheduling_sources, rig.now, priority="low")
    rig.db = Database(rig.db.path)
    rig.core = rig.new_core()
    next_high = high(rig)
    queue = rig.core.scheduling.ordered(rig.handle)
    assert queue[0]["task_id"] == oldest.id
    assert queue[1]["task_id"] == later.id
    view = rig.core.scheduling.explain(rig.handle, oldest.id)
    assert view["bypasses"] == 4 and view["fair_turn_due"]
    assert view["eligible_since"] == 1000 and view["wait_seconds"] == 1
    with pytest.raises(SchedulingBlocked):
        reserve(rig, next_high)
    assert reserve(rig, oldest).task_id == oldest.id


@pytest.mark.parametrize("boundary", ["expiry", "dependency", "hold", "reassignment"])
def test_ineligible_interval_ends_the_continuous_wait(boundary, rig):
    waiter = low(rig)
    admit_and_finish(rig, high(rig))
    assert rig.core.scheduling.explain(rig.handle, waiter.id)["bypasses"] == 1
    if boundary == "expiry":
        rig.now += 31
    elif boundary == "dependency":
        declare(rig.core, rig.handle, waiter, rig.scheduling_sources, rig.now,
                (Dependency("missing", "work_done"),), "low")
    else:
        proof = rig.proof("human_decision", waiter.id, {
            "previous_revision": waiter.decision_revision,
            "hold": True, "assignment": "other" if boundary == "reassignment" else "owner",
        })
        waiter = rig.core.decisions.record(rig.handle, waiter.id, waiter.version, proof, rig.key())
    before = rig.core.scheduling.explain(rig.handle, waiter.id)
    assert before["bypasses"] == 0 and before["eligible_since"] is None
    if boundary in {"hold", "reassignment"}:
        with pytest.raises(WorkError):
            reserve(rig, waiter)
        rig.now += 1
        proof = rig.proof("human_decision", waiter.id, {
            "previous_revision": waiter.decision_revision, "hold": False, "assignment": "owner",
        })
        waiter = rig.core.decisions.record(rig.handle, waiter.id, waiter.version, proof, rig.key())
        resumed = rig.core.scheduling.explain(rig.handle, waiter.id)
        assert resumed["bypasses"] == 0 and resumed["eligible_since"] == rig.now
    else:
        declare(rig.core, rig.handle, waiter, rig.scheduling_sources, rig.now, priority="low")
        current = rig.core.scheduling.explain(rig.handle, waiter.id)
        assert current["bypasses"] == 0 and not current["fair_turn_due"]


def test_capacity_failure_and_committed_replay_do_not_count_as_another_bypass(rig):
    waiter = low(rig)
    first, second, third = high(rig), high(rig), high(rig)
    reserve(rig, first)
    key = rig.key()
    proof = rig.decision(second, S.IN_PROGRESS)
    args = (rig.handle, second.id, "host", second.version, second.decision_revision, proof, key)
    attempt = rig.core.execution.reserve(*args)
    assert rig.core.execution.reserve(*args) == attempt
    with pytest.raises(CapacityExceeded):
        reserve(rig, third)
    view = rig.core.scheduling.explain(rig.handle, waiter.id)
    assert view["bypasses"] == 2 and not view["fair_turn_due"]


def test_priority_refresh_and_reads_preserve_the_wait_without_admission(rig):
    waiter = low(rig)
    admit_and_finish(rig, high(rig))
    rig.now += 5
    declare(rig.core, rig.handle, waiter, rig.scheduling_sources, rig.now, priority="normal")
    before = rig.core.notifications.list_events(rig.handle)
    for _ in range(2):
        view = rig.core.scheduling.explain(rig.handle, waiter.id)
        assert view["bypasses"] == 1 and view["eligible_since"] == 1000
    assert rig.core.notifications.list_events(rig.handle) == before


def test_blocked_head_does_not_block_independent_work_and_wait_projection_is_truthful(rig):
    blocked = high(rig)
    declare(rig.core, rig.handle, blocked, rig.scheduling_sources, rig.now,
            (Dependency("missing", "work_done"),), "urgent")
    ready = low(rig)
    other = low(rig)
    service = SimpleNamespace(core=rig.core, handle=rig.handle)
    view = task_view(service, other)
    assert view["observed_status"] == S.TODO
    assert view["activity"] == "waiting" and view["work_authorized"] is False
    assert view["scheduling"]["waiting_reason"] == "priority_order"
    assert rig.core.scheduling.ordered(rig.handle)[0]["task_id"] == ready.id
    assert reserve(rig, ready).task_id == ready.id
