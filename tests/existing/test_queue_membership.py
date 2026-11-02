"""Remote acknowledgement is distinct from explicit local queue membership."""

from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.domains.work.errors import InvalidTransition
from ai_task_manager.domains.work.models import Attempt, TaskProse
from ai_task_manager.storage import attempts, work
from tests.domains.helpers import Rig
from tests.linear.helpers import STATES

from .helpers import span
from .test_delivery_prose import human_decision, record_external


def prose_confirmation(rig, provider, task):
    prose = TaskProse("Verify the selected task prose", "Retain evidence without queuing new work.")
    revised = rig.core.intake.revise(
        rig.handle,
        task.id,
        prose,
        rig.approvals.add(rig.scope, prose),
        task.version,
        task.decision_revision,
        human_decision(rig, task),
        str(uuid4()),
    )
    assert revised.sync_state == "pending"
    return OutboundDrain(rig.core, rig.client, rig.journal).confirm_pending(rig.handle, task.id)


def remote_settlement(rig, provider, task):
    provider.issues[task.issue_id]["updatedAt"] = "2026-11-01T00:04:00Z"
    observation = record_external(rig, provider, task)
    assert observation["state"] == "pending"
    current = rig.core.get_task(rig.handle, task.id)
    key = str(uuid4())
    args = (
        rig.handle,
        task.id,
        observation["observation_id"],
        current.version,
        current.decision_revision,
        "enqueue",
        {},
    )
    settled = rig.core.external.settle(*args, key)
    before = rig.effects()
    assert rig.core.external.settle(*args, key) == settled and rig.effects() == before
    return settled


def assert_not_workable(rig, task):
    before = rig.effects()
    with pytest.raises(InvalidTransition, match="confirmed queue history"):
        rig.reserve(task)
    assert rig.effects() == before
    # An adversarial orphan reservation fixture reaches the real start destination
    # guard. Normal reserve has already refused to create this row.
    attempt = Attempt(
        str(uuid4()), task.id, task.session_id, "fixture", "reserved", task.decision_revision
    )
    with rig.db.transaction() as conn:
        attempts.insert(conn, attempt)
    key = str(uuid4())
    source = rig.startup_source(task, key, phase="start", attempt_id=attempt.id)
    startup = rig.runtime_proof(
        "worker_start",
        attempt.id,
        {"task_id": task.id, "host": "fixture", "worker_id": "synthetic-start-only"},
    )
    before = rig.effects()
    with pytest.raises(InvalidTransition, match="Destination evidence"):
        rig.core.execution.start(
            rig.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            startup,
            rig.decision(task, "In Progress"),
            key,
            readback_ref=source,
        )
    assert rig.effects() == before
    assert not rig.effects()["resource_leases"]
    with rig.db.transaction() as conn:
        conn.execute("DELETE FROM attempts WHERE id=?", (attempt.id,))


@pytest.mark.parametrize("path", [prose_confirmation, remote_settlement])
def test_ea83_imported_todo_acknowledgement_does_not_queue_then_explicit_enqueue_does(
    rig, provider, path
):
    task = rig.admit(provider.seed("Todo"))
    confirmed = path(rig, provider, task)
    assert confirmed.observed_status == "Todo" and not confirmed.queued_once
    assert not rig.effects()["attempts"] and not rig.effects()["resource_leases"]
    assert_not_workable(rig, confirmed)
    assert all(issue["state"]["name"] != "In Progress" for issue in provider.issues.values())
    key = str(uuid4())
    source = rig.intake.queue_readback(
        confirmed.id, confirmed.version, confirmed.decision_revision, key
    )
    args = (
        rig.handle,
        confirmed.id,
        "Todo",
        "enqueue",
        confirmed.version,
        confirmed.decision_revision,
        rig.decision(confirmed),
        {"existing_source": source},
    )
    queued = rig.core.transitions.transition(*args, key)
    assert queued.queued_once
    before = rig.effects()
    requests = len(provider.requests)
    assert rig.core.transitions.transition(*args, key) == queued
    assert rig.effects() == before and len(provider.requests) == requests
    assert (
        sum(
            event.kind == "existing_todo_queue_confirmed"
            for event in rig.core.notifications.list_events(rig.handle)
        )
        == 1
    )
    assert rig.reserve(queued).state == "reserved"


@pytest.mark.parametrize("path", [prose_confirmation, remote_settlement])
def test_ea83_previously_queued_imported_membership_is_sticky(rig, provider, path):
    task = rig.queue(rig.admit(provider.seed("Todo")))
    assert path(rig, provider, task).queued_once


@pytest.mark.parametrize("source,cause", [("Backlog", "enqueue"), ("In Review", "review_decision")])
def test_ea83_authorized_local_status_confirmation_establishes_imported_queue(
    rig, provider, source, cause
):
    task = rig.admit(provider.seed("Todo" if source == "In Review" else "Backlog"))
    if source == "In Review":
        task = rig.queue(task)
        # A retained own review lifecycle, not permission to import active work.
        provider.histories[task.issue_id][-1]["endedAt"] = "2026-11-01T00:01:30Z"
        provider.histories[task.issue_id].append(span(source, "2026-11-01T00:01:30Z", number=2))
        provider.issues[task.issue_id]["state"] = {"id": STATES[source], "name": source}
        with rig.db.transaction() as conn:
            task = work.save(
                conn, replace(task, observed_status=source, desired_status=source), task.version
            )
    key = str(uuid4())
    fresh = rig.intake.queue_readback(task.id, task.version, task.decision_revision, key)
    pending = rig.core.transitions.transition(
        rig.handle,
        task.id,
        "Todo",
        cause,
        task.version,
        task.decision_revision,
        human_decision(rig, task, "Todo"),
        {"existing_source": fresh},
        key,
    )
    assert pending.sync_state == "pending" and pending.queued_once == (source == "In Review")
    confirmed = OutboundDrain(rig.core, rig.client, rig.journal).confirm_pending(
        rig.handle, task.id
    )
    assert confirmed.queued_once and confirmed.observed_status == "Todo"


@pytest.mark.parametrize("path", ["prose", "settle"])
def test_ea83_nonimported_todo_projection_is_preserved(tmp_path, path):
    rig = Rig(tmp_path / "standalone.sqlite")
    task = rig.queued()
    # Pin the existing behavior independently of the new imported-record guard.
    with rig.db.transaction() as conn:
        task = work.save(conn, replace(task, queued_once=False), task.version)
    if path == "prose":
        prose = TaskProse("Verify original acknowledgement", "Keep nonimported behavior intact.")
        revised = rig.core.intake.revise(
            rig.handle,
            task.id,
            prose,
            rig.approval(prose),
            task.version,
            task.decision_revision,
            rig.decision(task, "Todo"),
            rig.key(),
        )
        result = rig.confirm(revised)
    else:
        ref = rig.proof(
            "external_observation",
            task.id,
            {
                "issue_id": task.issue_id,
                "source_revision": "2026-11-01T00:04:00Z",
                "status": "Todo",
                "title": task.title,
                "body": task.body,
                "assigned_to_owner": True,
                "actor_kind": "human",
                "actor_authorized": True,
            },
        )
        observed = rig.core.external.observe(
            rig.handle, task.id, task.version, task.decision_revision, ref, rig.key()
        )
        current = rig.current(task)
        result = rig.core.external.settle(
            rig.handle,
            task.id,
            observed["observation_id"],
            current.version,
            current.decision_revision,
            "enqueue",
            {},
            rig.key(),
        )
    assert result.queued_once
