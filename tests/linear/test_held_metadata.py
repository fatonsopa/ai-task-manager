"""Signed label metadata preserves held authority; lifecycle facts still fence it."""

import json

import pytest

from ai_task_manager.application.service.facade import ApplicationService
from tests.linear.helpers import STATES, uid
from tests.linear.test_decision_publication import env as env
from tests.linear.test_decision_publication import request


def held(env):
    rig, task, publisher, approvals = env
    task = rig.transition(task, "Todo", "enqueue")
    task, attempt = rig.running(task)
    task, _ = rig.settle(task, attempt, state="paused")
    rig.provider.edit(task.issue_id, state={"id": STATES["On Hold"], "name": "On Hold"})
    event = rig.provider.event(task.issue_id)
    event["updatedFrom"] = {"stateId": STATES["In Progress"]}
    headers, body = rig.signed(event)
    assert rig.receiver.receive("POST", headers, body).status == 200
    result = rig.inbound.run_once(rig.handle)
    assert result["state"] == "applied", result
    task = rig.current(task)
    assert task.human_hold and task.observed_status == "On Hold"
    return rig, task, publisher, approvals


def test_own_decision_label_event_preserves_hold_and_publication_context(env):
    rig, task, publisher, approvals = held(env)
    received = []
    previous = rig.provider.issues[task.issue_id]["updatedAt"]

    def label_echo(query, _):
        if "mutation DecisionLabelsUpdate" not in query["query"]:
            return
        event = rig.provider.event(task.issue_id)
        event["updatedFrom"] = {"labelIds": [], "updatedAt": previous}
        headers, body = rig.signed(event)
        assert rig.receiver.receive("POST", headers, body).status == 200
        received.append((headers, body, rig.inbound.run_once(rig.handle)))

    rig.provider.hook = label_echo
    result = request((rig, task, publisher, approvals))
    assert result["state"] == result["label_state"] == "confirmed"
    assert received[0][2]["classification"] == "held_metadata"
    current = rig.current(task)
    assert current == task and current.human_hold
    assert rig.receiver.receive("POST", received[0][0], received[0][1]).status == 200
    assert rig.inbound.run_once(rig.handle) is None and rig.current(task) == task


@pytest.mark.parametrize(
    "fault", ["state", "prose", "assignment", "delegate", "actor", "missing_delta", "reorder"]
)
def test_held_metadata_shortcut_refuses_changed_or_unknown_decision_facts(env, fault):
    rig, task, _, _ = held(env)
    changes = {}
    if fault == "prose":
        changes["title"] = "Changed held scope"
    if fault == "assignment":
        changes["assignee"] = {"id": uid(800)}
    if fault == "delegate":
        changes["delegate"] = {"id": uid(801), "app": True}
    rig.provider.edit(task.issue_id, **changes)
    event = rig.provider.event(task.issue_id)
    event["updatedFrom"] = {"labelIds": []}
    if fault == "state":
        event["updatedFrom"]["stateId"] = STATES["On Hold"]
    if fault == "actor":
        event["actor"] = {"type": "integration", "id": uid(802)}
    if fault == "missing_delta":
        del event["updatedFrom"]
    if fault == "reorder":
        event["data"]["updatedAt"] = rig.journal.latest_snapshot(rig.scope, task.issue_id)[
            "revision"
        ]
    headers, body = rig.signed(event)
    assert rig.receiver.receive("POST", headers, body).status == 200
    result = rig.inbound.run_once(rig.handle)
    assert result.get("classification") != "held_metadata"
    assert len(rig.core.external.observations(rig.handle, task.id)) == 2
    assert rig.current(task).human_hold
    current = rig.current(task)
    if fault in {"assignment", "delegate"}:
        # Withdrawal fences the old authority without inventing a human choice
        # from the privacy-minimized, unattributed scope-removal observation.
        assert current.decision_revision == task.decision_revision
        assert current.version == task.version + 1 and current.sync_state == "uncertain"
        assert current.decision_ref != task.decision_ref
        assert current.title == task.title and current.body == task.body
        observations = rig.core.external.observations(rig.handle, task.id)
        latest = json.loads(observations[-1]["payload"])
        assert latest["assigned_to_owner"] is False
        assert latest["actor_kind"] == "integration" and latest["actor_authorized"] is False
        projected = ApplicationService(rig.core, rig.handle).call("task_show", {"task_id": task.id})
        assert projected["ok"] and projected["data"]["quarantined"]
        assert projected["data"]["body"] is None and not projected["data"]["active_group"]
        assert rig.receiver.receive("POST", headers, body).status == 200
        assert rig.inbound.run_once(rig.handle) is None and rig.current(task) == current
    elif fault not in {"actor", "reorder"}:
        assert current.decision_revision == task.decision_revision + 1
