"""Preserve the original start intent and retry only proven zero-reservation work."""

import json

import pytest

from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work.models import TaskProse
from tests.workflows.helpers import WorkflowFixture
from tests.workflows.test_driver import LostReply


@pytest.fixture
def wf(tmp_path):
    value = WorkflowFixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


def pending_start(wf):
    result = wf.new_driver(faults=LostReply("before:start")).run(wf.ref)
    assert result["state"] == "blocked"
    row = wf.store.read(wf.ref, wf.rig.scope)
    assert row["attempt_id"] is None and row["steps"]["start"]["result"] is None
    assert not wf.runtime.dispatches
    return row


def test_wr01_zero_reservation_retry_reuses_original_intent_decision_and_operation(wf, monkeypatch):
    before = pending_start(wf)
    calls = []
    start = wf.runtime.start

    def observed(handle, task, plan, decision, key):
        calls.append((task.version, task.decision_revision, decision, key, plan.deadline))
        return start(handle, task, plan, decision, key)

    monkeypatch.setattr(wf.runtime, "start", observed)
    driver = wf.new_driver()
    monkeypatch.setattr(
        driver, "_decision", lambda *args: pytest.fail("Do not replace saved decision")
    )
    assert driver.run(wf.ref)["state"] == "awaiting_review"
    after = wf.store.read(wf.ref, wf.rig.scope)
    assert after["steps"]["start"]["payload"] == before["steps"]["start"]["payload"]
    assert after["steps"]["start"]["result"]["reconciled"] is True
    assert calls == [
        (
            wf.task.version,
            wf.task.decision_revision,
            before["steps"]["start"]["payload"]["decision_ref"],
            wf.ref + ":start",
            wf.plan.deadline,
        )
    ]
    assert len(wf.runtime.dispatches) == 1
    assert driver.run(wf.ref)["state"] == "awaiting_review"
    assert len(calls) == 1


def test_wr02_actual_pre_reserve_block_remains_saved_until_same_start_succeeds(wf, monkeypatch):
    start = wf.runtime.start
    calls = []

    def blocked(handle, task, plan, decision, key):
        calls.append((task.version, decision, key))
        if len(calls) == 1:
            raise SchedulingBlocked("Authored provider dependency failure before reserve")
        return start(handle, task, plan, decision, key)

    monkeypatch.setattr(wf.runtime, "start", blocked)
    first = wf.driver.run(wf.ref)
    assert first["reason"] == "SchedulingBlocked"
    before = wf.store.read(wf.ref, wf.rig.scope)
    assert before["attempt_id"] is None and not wf.runtime.dispatches
    assert wf.new_driver().run(wf.ref)["state"] == "awaiting_review"
    after = wf.store.read(wf.ref, wf.rig.scope)
    assert before["steps"]["start"]["payload"] == after["steps"]["start"]["payload"]
    assert calls[0] == calls[1] and len(wf.runtime.dispatches) == 1


@pytest.mark.parametrize("state", ["reserved", "unknown"])
def test_wr03_any_canonical_reservation_keeps_unknown_start_uncertain(wf, state):
    before = pending_start(wf)
    task = wf.rig.current(wf.task)
    attempt = wf.rig.core.execution.reserve(
        wf.rig.handle,
        task.id,
        "fixture",
        task.version,
        task.decision_revision,
        before["steps"]["start"]["payload"]["decision_ref"],
        wf.ref + ":start:reserve",
    )
    if state == "unknown":
        evidence = wf.rig.core.context.evidence
        wf.rig.core.context.evidence = wf.rig.proofs
        try:
            attempt = wf.rig.settle(wf.rig.current(task), attempt, "unknown")[1]
        finally:
            wf.rig.core.context.evidence = evidence
    result = wf.new_driver().run(wf.ref)
    assert result["state"] == "uncertain" and result["reason"] == "unfinished_start"
    assert wf.store.read(wf.ref, wf.rig.scope)["steps"] == before["steps"]
    assert not wf.runtime.dispatches


@pytest.mark.parametrize("cursor", [-1, True, "8", 1000000, 0])
def test_wr04_invalid_or_missing_original_cursor_does_not_authorize_retry(wf, cursor):
    row = pending_start(wf)
    payload = dict(row["steps"]["start"]["payload"], cursor=cursor)
    with wf.store.transaction() as conn:
        conn.execute("UPDATE steps SET payload=? WHERE name='start'", (json.dumps(payload),))
    assert wf.new_driver().run(wf.ref)["state"] == "uncertain"
    assert not wf.runtime.dispatches


def test_wr05_changed_task_version_cannot_rebind_original_start(wf):
    before = pending_start(wf)
    r, task = wf.rig, wf.rig.current(wf.task)
    prose = TaskProse(task.title, task.body, task.code_bearing, task.required_work)
    changed = r.core.intake.revise(
        r.handle,
        task.id,
        prose,
        r.approval(prose),
        task.version,
        task.decision_revision,
        r.decision(task, "Todo"),
        r.key(),
    )
    changed = r.confirm(changed)
    assert changed.version > task.version
    assert changed.prose_digest == task.prose_digest
    assert changed.decision_revision == task.decision_revision
    assert wf.new_driver().run(wf.ref)["state"] == "uncertain"
    assert wf.store.read(wf.ref, r.scope)["steps"] == before["steps"]
    assert not wf.runtime.dispatches


def test_wr06_original_deadline_still_checkpoints_pending_start(wf):
    before = pending_start(wf)
    wf.rig.now = wf.plan.deadline
    assert wf.new_driver().run(wf.ref)["reason"] == "invocation_deadline"
    assert wf.store.read(wf.ref, wf.rig.scope)["steps"] == before["steps"]
    assert not wf.runtime.dispatches
