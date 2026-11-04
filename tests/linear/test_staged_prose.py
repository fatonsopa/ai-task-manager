import json

import pytest

from ai_task_manager.domains.work.errors import ApprovalRequired, EvidenceRequired
from ai_task_manager.domains.work.models import TaskProse
from tests.linear.helpers import LinearRig, ProviderFixture


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def staged(rig, running=False):
    if running:
        task, attempt = rig.running()
    else:
        task, attempt = rig.task(), None
    prose = TaskProse(
        "Verify the expanded scope",
        "Run `check --expanded` and retain evidence.",
        True,
        ("expanded",),
    )
    rig.provider.edit(task.issue_id, title=prose.title, description=prose.body)
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    observation = rig.core.external.observations(rig.handle, task.id)[-1]
    return rig.current(task), attempt, prose, observation


def test_external_prose_is_staged_as_an_event_without_adoption_or_overwrite(rig):
    task, _, prose, observation = staged(rig)
    assert task.title != prose.title and task.human_hold and task.sync_state == "uncertain"
    event = rig.core.notifications.list_events(rig.handle)[-1]
    assert event.kind == "external_prose_change_staged"
    assert event.payload["requires_writer_approval"] is True
    assert json.loads(observation["payload"])["body"] == prose.body
    assert rig.provider.issues[task.issue_id]["description"] == prose.body


def test_approved_external_prose_needs_fresh_readback_and_separate_status_settlement(rig):
    task, _, prose, observation = staged(rig)
    ref = rig.inbound.fresh_readback_ref(rig.handle, task.id, observation["id"])
    with pytest.raises(ApprovalRequired):
        rig.core.external.approve_prose_revision(
            rig.handle,
            task.id,
            observation["id"],
            prose,
            None,
            task.version,
            task.decision_revision,
            ref,
            rig.key(),
        )
    approval = rig.approval(prose)
    with pytest.raises(EvidenceRequired):
        rig.core.external.approve_prose_revision(
            rig.handle,
            task.id,
            observation["id"],
            prose,
            approval,
            task.version,
            task.decision_revision,
            None,
            rig.key(),
        )
    rig.now += 31
    with pytest.raises(EvidenceRequired):
        rig.core.external.approve_prose_revision(
            rig.handle,
            task.id,
            observation["id"],
            prose,
            approval,
            task.version,
            task.decision_revision,
            ref,
            rig.key(),
        )
    fresh = rig.inbound.fresh_readback_ref(rig.handle, task.id, observation["id"])
    task = rig.core.external.approve_prose_revision(
        rig.handle,
        task.id,
        observation["id"],
        prose,
        approval,
        task.version,
        task.decision_revision,
        fresh,
        rig.key(),
    )
    assert (
        task.body == prose.body
        and task.required_work == ("expanded",)
        and task.completed_work == ()
    )
    assert task.human_hold and task.sync_state == "uncertain"
    task = rig.core.external.settle(
        rig.handle,
        task.id,
        observation["id"],
        task.version,
        task.decision_revision,
        "dequeue",
        {},
        rig.key(),
    )
    assert task.sync_state == "confirmed" and task.human_hold


def test_active_or_unresolved_worker_blocks_prose_adoption_until_real_quiescence(rig):
    task, attempt, prose, observation = staged(rig, running=True)
    ref = rig.inbound.fresh_readback_ref(rig.handle, task.id, observation["id"])
    approval = rig.approval(prose)
    with pytest.raises(EvidenceRequired):
        rig.core.external.approve_prose_revision(
            rig.handle,
            task.id,
            observation["id"],
            prose,
            approval,
            task.version,
            task.decision_revision,
            ref,
            rig.key(),
        )
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "running"
    task, _ = rig.settle(task, attempt, "paused")
    fresh = rig.inbound.fresh_readback_ref(rig.handle, task.id, observation["id"])
    updated = rig.core.external.approve_prose_revision(
        rig.handle,
        task.id,
        observation["id"],
        prose,
        approval,
        task.version,
        task.decision_revision,
        fresh,
        rig.key(),
    )
    assert updated.body == prose.body and updated.human_hold
