from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from ai_task_manager.domains.scheduling.models import Dependency, SchedulingBlocked
from ai_task_manager.domains.scheduling.policy import ordered
from ai_task_manager.domains.work.errors import WorkError
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import work

from .helpers import declare


def reserve(rig, task, core=None, handle=None, scope=None):
    return (core or rig.core).execution.reserve(
        handle or rig.handle, task.id, "host", task.version, task.decision_revision,
        rig.decision(task, S.IN_PROGRESS, scope=scope), rig.key(),
    )


def blocked(rig, task):
    return declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now,
                   (Dependency("missing-predecessor", "work_done"),))


def active_count(rig):
    with rig.db.transaction() as conn:
        return conn.execute("SELECT count(*) FROM attempts WHERE active=1").fetchone()[0]


@pytest.mark.parametrize("variant", ["unknown", "unmet", "satisfied", "explicit_empty"])
def test_reservation_requires_complete_dependencies(variant, rig):
    predecessor = None
    if variant == "satisfied":
        predecessor, attempt = rig.running()
        predecessor = rig.complete(predecessor)
    task = rig.queued()
    if variant == "unknown":
        with rig.db.transaction() as conn:
            conn.execute("DELETE FROM scheduling_snapshots WHERE task_id=?", (task.id,))
    elif variant == "unmet":
        blocked(rig, task)
    elif variant == "satisfied":
        proof = rig.proof("work_unit_complete", predecessor.id,
                          {"unit": "complete", "prose_digest": predecessor.prose_digest})
        declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now,
                (Dependency(predecessor.id, "unit_verified", "complete",
                            (("work_unit_complete", proof),)),))
    before = active_count(rig)
    if variant in {"unknown", "unmet"}:
        with pytest.raises(SchedulingBlocked):
            reserve(rig, task)
        assert active_count(rig) == before
    else:
        assert reserve(rig, task).task_id == task.id
        assert active_count(rig) == before + 1


@pytest.mark.parametrize("variant", ["reserve", "start", "running", "validate_work_lease", "work_boundary", "unqueued_orphan_start_guard_precedence"])
def test_recheck_each_work_boundary(variant, rig):
    if variant == "unqueued_orphan_start_guard_precedence":
        from ai_task_manager.domains.work.errors import InvalidTransition
        from ai_task_manager.domains.work.models import Attempt
        from ai_task_manager.storage import attempts
        task = rig.task()
        with rig.db.transaction() as conn:
            task = work.save(conn, replace(task, observed_status=S.TODO, desired_status=S.TODO), task.version)
            attempt = Attempt("authored-orphan", task.id, task.session_id, "host", "reserved", 0)
            attempts.insert(conn, attempt)
            conn.execute("DELETE FROM scheduling_snapshots WHERE task_id=?", (task.id,))
        def history():
            with rig.db.transaction() as conn:
                return {name: [tuple(row) for row in conn.execute("SELECT * FROM " + name)]
                        for name in ("tasks", "attempts", "resource_leases", "events", "outbox", "operations")}
        before = history()
        startup = rig.proof("worker_start", attempt.id, {
            "task_id": task.id, "host": "host", "worker_id": "authored-worker"})
        with pytest.raises(InvalidTransition, match="Destination evidence"):
            rig.core.execution.start(rig.handle, attempt.id, task.version, task.decision_revision,
                startup, rig.decision(task, S.IN_PROGRESS), rig.key())
        assert history() == before
        return
    attempt = None
    if variant == "reserve":
        task = rig.queued()
    elif variant == "start":
        task, attempt = rig.reserved()
    elif variant == "running":
        task, attempt = rig.starting()
        task = rig.confirm(task)
    else:
        task, attempt = rig.running()
    blocked(rig, task)
    before = active_count(rig)
    with pytest.raises(SchedulingBlocked):
        if variant == "reserve":
            reserve(rig, task)
        elif variant == "start":
            proof = rig.proof("worker_start", attempt.id, {
                "task_id": task.id, "host": "host", "worker_id": "worker"
            })
            rig.core.execution.start(rig.handle, attempt.id, task.version, task.decision_revision,
                                     proof, rig.decision(task, S.IN_PROGRESS), rig.key())
        elif variant == "running":
            rig.core.execution.running(rig.handle, attempt.id, task.version, task.decision_revision,
                                       rig.resources(task, attempt), rig.key())
        elif variant == "validate_work_lease":
            rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)
        else:
            with rig.core.execution.work_boundary(rig.handle, attempt.id, attempt.version):
                pytest.fail("Fenced authority yielded a work effect")
    assert active_count(rig) == before


@pytest.mark.parametrize("variant", ["human_hold", "unqueued", "remote_todo_only", "delivered", "terminal", "review_without_human"])
def test_holds_queue_and_delivery_survive(variant, rig):
    task = rig.task() if variant in {"unqueued", "remote_todo_only"} else rig.queued()
    actor = "human"
    if variant == "human_hold":
        proof = rig.proof("human_decision", task.id, {"previous_revision": 0, "hold": True})
        task = rig.core.decisions.record(rig.handle, task.id, task.version, proof, rig.key())
    elif variant == "remote_todo_only":
        # Authored remote fact: the status label exists, but no queue intent does.
        with rig.db.transaction() as conn:
            task = work.save(conn, replace(task, observed_status=S.TODO, desired_status=S.TODO), task.version)
    elif variant in {"delivered", "review_without_human"}:
        task, attempt = rig.running(task)
        task, _ = rig.settle(task, attempt)
        task = rig.complete(task)
        if variant == "delivered":
            task = rig.transition(task, S.IN_DEV, "merge", rig.delivery(task))
        else:
            task = rig.transition(task, S.IN_REVIEW, "request_review")
            actor = "automation"
    elif variant == "terminal":
        task = rig.transition(task, S.CANCELED, "cancel")
    before = active_count(rig)
    with pytest.raises(WorkError):
        rig.core.execution.reserve(rig.handle, task.id, "host", task.version,
            task.decision_revision, rig.decision(task, S.IN_PROGRESS, actor=actor), rig.key())
    assert active_count(rig) == before


@pytest.mark.parametrize("variant", ["higher_ready", "higher_blocked", "equal_queue_order", "same_order_id_tie", "capacity_full", "remote_todo_precedes_explicit_queue", "canonical_delivered_queue_head", "canonical_terminal_queue_head", "canonical_corrupt_queue_head"])
def test_priority_and_stable_ties(variant, rig):
    if variant.startswith("canonical_"):
        canonical_queue_head_control(rig, variant)
        return
    if variant == "same_order_id_tie":
        assert [item[0] for item in ordered([("b", "normal", 42), ("a", "normal", 42)])] == ["a", "b"]
        return
    if variant == "remote_todo_precedes_explicit_queue":
        from ai_task_manager.storage import events, scheduling
        first = rig.task()
        with rig.db.transaction() as conn:
            events.append(conn, first.session_id, first.id, "provider_status_confirmed", {
                "observed_status": "Todo", "task_version": 999, "decision_revision": 0,
            })
            assert scheduling.queue_sequence(conn, first.id) is None
        second = rig.queued()
        first = rig.transition(first, S.TODO, "enqueue")
        with pytest.raises(SchedulingBlocked):
            reserve(rig, first)
        assert reserve(rig, second).task_id == second.id
        return
    first, second = rig.queued(), rig.queued()
    if variant.startswith("higher"):
        declare(rig.core, rig.handle, first, rig.scheduling_sources, rig.now, priority="low")
        edges = (Dependency("unknown", "work_done"),) if variant == "higher_blocked" else ()
        declare(rig.core, rig.handle, second, rig.scheduling_sources, rig.now, edges, "urgent")
        if variant == "higher_ready":
            with pytest.raises(SchedulingBlocked):
                reserve(rig, first)
            assert reserve(rig, second).task_id == second.id
        else:
            assert reserve(rig, first).task_id == first.id
    else:
        with pytest.raises(SchedulingBlocked):
            reserve(rig, second)
        reserve(rig, first)
        if variant == "capacity_full":
            reserve(rig, second)
            third = rig.queued()
            with pytest.raises(WorkError):
                reserve(rig, third)
            assert active_count(rig) == 2


@pytest.mark.parametrize("variant", ["new_higher_priority", "priority_refresh"])
def test_priority_does_not_preempt_active_work(variant, rig):
    task, attempt = rig.running()
    if variant == "new_higher_priority":
        other = rig.queued()
        declare(rig.core, rig.handle, other, rig.scheduling_sources, rig.now, priority="urgent")
    else:
        declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now, priority="low")
    assert rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version) == attempt


@pytest.mark.parametrize("variant", ["same_task_two_supervisors", "capacity_race", "dependency_invalidated_first", "reserve_first_then_invalidate"])
def test_concurrent_claims_and_graph_changes(variant, rig):
    task = rig.queued()
    if variant == "dependency_invalidated_first":
        blocked(rig, task)
        with pytest.raises(SchedulingBlocked):
            reserve(rig, task)
        assert active_count(rig) == 0
        return
    if variant == "reserve_first_then_invalidate":
        attempt = reserve(rig, task)
        task = rig.current(task)
        blocked(rig, task)
        assert active_count(rig) == 1
        with rig.db.transaction() as conn:
            assert conn.execute("SELECT invalidated_by FROM scheduling_attempts WHERE attempt_id=?", (attempt.id,)).fetchone()[0]
        return
    if variant == "capacity_race":
        reserve(rig, task)
        first = rig.queued()
        scope = replace(rig.scope, logical_session="independent-queue")
        handle = rig.session(scope)
        second = rig.task(handle=handle, confirmed=False)
        second = rig.confirm(second, handle=handle, scope=scope)
        second = rig.core.transitions.transition(handle, second.id, S.TODO, "enqueue",
            second.version, 0, rig.decision(second, S.TODO, scope=scope), {}, rig.key())
        second = rig.confirm(second, handle=handle, scope=scope)
        targets = [(rig.handle, rig.scope, first), (handle, scope, second)]
        barrier = Barrier(2)
        cores = [rig.new_core(), rig.new_core()]
        keys = [rig.key(), rig.key()]
        decisions = [rig.decision(t, S.IN_PROGRESS, scope=s) for h, s, t in targets]
        def capacity_claim(index):
            h, s, t = targets[index]
            barrier.wait(timeout=5)
            try:
                return cores[index].execution.reserve(h, t.id, "host", t.version,
                    t.decision_revision, decisions[index], keys[index])
            except WorkError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(capacity_claim, (0, 1)))
        assert sum(value is not None for value in results) == 1
        assert active_count(rig) == 2
        return
    cores, barrier = [rig.new_core(), rig.new_core()], Barrier(2)
    decision = rig.decision(task, S.IN_PROGRESS)
    keys = [rig.key(), rig.key()]
    def claim(index):
        barrier.wait(timeout=5)
        try:
            return cores[index].execution.reserve(rig.handle, task.id, "host", task.version,
                task.decision_revision, decision, keys[index])
        except WorkError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))
    assert sum(value is not None for value in results) == 1
    assert active_count(rig) == 1


@pytest.mark.parametrize("variant", ["blocked_then_refresh", "graph_changed", "source_revoked", "process_unknown"])
def test_sticky_attempt_invalidation(variant, rig):
    task, attempt = rig.running()
    if variant == "process_unknown":
        rig.settle(task, attempt, "unknown", quiescent=False)
    else:
        if variant == "source_revoked":
            declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now, completeness="unavailable")
        else:
            blocked(rig, task)
        declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now)
    with pytest.raises(WorkError):
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)
    assert active_count(rig) == 1
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)).fetchone()[0] == 2


@pytest.mark.parametrize("variant", ["on_hold", "paused_attempt", "failed_attempt", "no_prior_queue"])
def test_resume_rechecks_dependencies(variant, rig):
    if variant == "no_prior_queue":
        task = rig.task()
    else:
        task, attempt = rig.running()
        task, _ = rig.settle(task, attempt, "failed" if variant == "failed_attempt" else "paused")
        if variant == "on_hold":
            task = rig.transition(task, S.ON_HOLD, "hold")
    blocked(rig, task)
    with pytest.raises(WorkError):
        reserve(rig, task)
    assert active_count(rig) == 0


def canonical_queue_head_control(rig, variant):
    from types import SimpleNamespace
    from ai_task_manager.application.service.projection import task_view
    from ai_task_manager.domains.work.errors import InvalidTransition, SchemaError
    from ai_task_manager.storage import issue_claims

    closed, valid = rig.queued(), rig.queued()
    declare(rig.core, rig.handle, closed, rig.scheduling_sources, rig.now, priority="urgent")
    assert closed.observed_status == S.TODO and closed.delivered_once is False
    with rig.db.transaction() as conn:
        issue_claims.fence(conn, rig.scope, closed.issue_id,
            delivered=variant != "canonical_terminal_queue_head",
            terminal=variant == "canonical_terminal_queue_head", source="authored-known-history")
        if variant == "canonical_corrupt_queue_head":
            conn.execute("UPDATE issue_lifecycle SET delivered=2")
    service = SimpleNamespace(core=rig.core, handle=rig.handle)
    if variant == "canonical_corrupt_queue_head":
        with pytest.raises(SchemaError):
            task_view(service, closed)
        with pytest.raises(SchemaError):
            reserve(rig, closed)
        with pytest.raises(SchemaError):
            reserve(rig, valid)
        assert active_count(rig) == 0
        return
    view = task_view(service, closed)
    assert view["observed_status"] == S.TODO  # Retain the observed cache until sync settles it.
    assert view["activity"] == "blocked" and view["work_authorized"] is False
    blocker = view["scheduling"]["blockers"][0]
    assert blocker["code"] == "issue_lifecycle_closed"
    assert "new linked" in blocker["release_condition"] and "issue" in blocker["release_condition"]
    with pytest.raises(InvalidTransition):
        reserve(rig, closed)
    assert active_count(rig) == 0
    assert reserve(rig, valid).task_id == valid.id
    assert active_count(rig) == 1
    assert rig.current(closed) == closed
