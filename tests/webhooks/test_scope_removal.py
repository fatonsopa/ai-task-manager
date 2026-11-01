import json

import pytest

from tests.linear.helpers import LinearRig, ProviderFixture, uid


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


@pytest.mark.parametrize("moved_field", ["team", "project"])
def test_l16_bound_issue_scope_move_retains_projection_and_revokes_without_adoption(
    rig, moved_field
):
    task, attempt = rig.running()
    issue = rig.provider.edit(
        task.issue_id,
        **{
            moved_field: {"id": uid(900), "key": "FOREIGN"},
            "title": "Foreign private title",
            "description": "Foreign private body",
        },
    )
    payload = rig.provider.event(
        task.issue_id,
        actor={"type": "integration", "id": uid(901), "email": "private@example.invalid"},
    )
    payload["data"].update(
        title=issue["title"],
        description=issue["description"],
        projectName="Foreign private project",
    )
    headers, body = rig.signed(payload)
    assert rig.receiver.receive("POST", headers, body).status == 200
    stored = rig.journal.deliveries(rig.scope)[0]
    assert "Foreign private" not in stored["payload"] and "email" not in stored["payload"]
    assert uid(900) not in stored["payload"]
    assert json.loads(stored["payload"])["data"]["scopeRemoval"] is True
    result = rig.inbound.run_once(rig.handle)
    assert result["state"] == "conflict" and result["error"] == "registered_issue_scope_removed"
    current = rig.current(task)
    assert (
        current.human_hold
        and current.title == task.title
        and current.observed_status == "In Progress"
    )
    assert current.decision_revision == 0
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "running"


def test_l16_repeated_scope_movement_and_gap_recovery_do_not_import_foreign_data(rig):
    task = rig.task()
    for project_id in (uid(900), uid(901)):
        rig.provider.edit(
            task.issue_id,
            project={"id": project_id},
            title="Foreign title",
            description="Foreign body",
        )
        headers, body = rig.signed(rig.provider.event(task.issue_id))
        assert rig.receiver.receive("POST", headers, body).status == 200
        assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    assert len(rig.core.list_tasks(rig.handle)) == 1
    assert len(rig.core.external.observations(rig.handle, task.id)) == 2
    assert rig.reconcile.inspect_registered(rig.handle) == [
        {"task_id": task.id, "state": "scope_conflict"}
    ]
    assert rig.current(task).title == task.title


def test_l16_missed_scope_move_still_fences_old_authority_during_registered_recovery(rig):
    task, attempt = rig.running()
    rig.provider.edit(task.issue_id, project={"id": uid(900)}, title="Foreign private title")
    assert rig.reconcile.inspect_registered(rig.handle)[0]["state"] == "scope_conflict"
    assert rig.current(task).human_hold and rig.current(task).decision_revision == 0
