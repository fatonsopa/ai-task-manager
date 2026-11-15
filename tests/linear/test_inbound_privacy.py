"""Authored signed delivery/readback controls inspect physical retention, not projection."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.linear.helpers import LinearRig, ProviderFixture, uid


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def withdrawal(rig, task, kind, marker):
    changed = {"title": marker + " TITLE", "description": marker + " BODY"}
    if kind == "delegate":
        changed["delegate"] = {"id": uid(900), "app": True}
    else:
        changed["assignee"] = None if kind == "unassigned" else {"id": uid(901)}
    return rig.provider.edit(task.issue_id, **changed)


def assert_private(rig, task, marker, history):
    for path in (rig.journal.path, rig.db.path):
        assert marker.encode() not in Path(path).read_bytes(), str(path)
    assert rig.journal.sends(rig.scope) == history
    current = rig.current(task)
    assert current.title == task.title and current.body == task.body and current.human_hold
    rows = rig.core.external.observations(rig.handle, task.id)
    assert rows
    latest = json.loads(rows[-1]["payload"])
    assert latest["assigned_to_owner"] is False and latest["actor_authorized"] is False
    assert latest["title"] == task.title and latest["body"] == task.body


@pytest.mark.parametrize("kind", ["assignee", "unassigned", "delegate"])
@pytest.mark.parametrize("route", ["signed", "readback"])
def test_withdrawn_inbound_never_persists_new_foreign_prose(rig, kind, route):
    task = rig.task()
    # InstalledReceiver supplies this current immutable binding after connection
    # verification; LinearRig's older receiver fixture captured its prior binding.
    rig.receiver.binding = rig.client.binding
    history = rig.journal.sends(rig.scope)
    marker = "NEW FOREIGN PRIVATE CONTENT " + kind + route
    if route == "signed":
        issue = withdrawal(rig, task, kind, marker)
        event = rig.provider.event(task.issue_id)
        event["data"].update(
            title=issue["title"],
            description=issue["description"],
            delegateId=issue["delegate"]["id"],
        )
    else:
        event = rig.provider.event(task.issue_id)
    headers, body = rig.signed(event)
    assert rig.receiver.receive("POST", headers, body).status == 200
    assert marker.encode() not in Path(rig.journal.path).read_bytes()
    if route == "readback":
        withdrawal(rig, task, kind, marker)
    result = rig.inbound.run_once(rig.handle)
    assert result["state"] == "conflict"
    assert_private(rig, task, marker, history)
    row = rig.journal.deliveries(rig.scope)[0]
    assert marker not in row["payload"] and marker not in row["checkpoint"]
    if route == "signed" and kind == "assignee":
        before = rig.journal.deliveries(rig.scope)
        assert rig.receiver.receive("POST", headers, body).status == 200
        assert rig.inbound.run_once(rig.handle) is None
        changed = copy.deepcopy(event)
        changed["data"]["description"] += " DIFFERENT"
        retry_headers, retry_body = rig.signed(changed, dict(headers)["Linear-Delivery"])
        assert rig.receiver.receive("POST", retry_headers, retry_body).status == 409
        assert rig.journal.deliveries(rig.scope) == before


@pytest.mark.parametrize("kind", ["assignee", "unassigned"])
def test_missed_assignment_withdrawal_redacts_poll_observation(rig, kind):
    task = rig.task()
    history = rig.journal.sends(rig.scope)
    marker = "POLL NEW FOREIGN PRIVATE CONTENT " + kind
    withdrawal(rig, task, kind, marker)
    result = rig.reconcile.inspect_registered(rig.handle)
    assert_private(rig, task, marker, history)
    assert result == [{"task_id": task.id, "state": "scope_conflict"}]
    assert not rig.journal.deliveries(rig.scope)


def test_explicit_delegate_without_verified_selection_refuses_before_retention(rig):
    task = rig.task()
    marker = "EXPLICIT UNVERIFIED DELEGATE PRIVATE CONTENT"
    issue = withdrawal(rig, task, "delegate", marker)
    event = rig.provider.event(task.issue_id)
    event["data"].update(
        title=issue["title"], description=issue["description"], delegateId=issue["delegate"]["id"]
    )
    rig.receiver.binding = replace(rig.client.binding, connection=None)
    headers, body = rig.signed(event)
    assert rig.receiver.receive("POST", headers, body).status == 403
    assert not rig.journal.deliveries(rig.scope)
    assert not rig.core.sync.inbound(rig.handle)
    assert rig.current(task) == task
    event["data"]["id"] = uid(902)
    unknown_headers, unknown_body = rig.signed(event)
    assert rig.receiver.receive("POST", unknown_headers, unknown_body).status == 200
    assert not rig.journal.deliveries(rig.scope) and len(rig.journal.ignored(rig.scope)) == 1
    assert not rig.core.sync.inbound(rig.handle)
    for path in (rig.journal.path, rig.db.path):
        assert marker.encode() not in Path(path).read_bytes()


def test_outbound_preflight_withdrawal_retains_intent_without_foreign_prose(rig):
    task = rig.task()
    pending = rig.transition(task, "Todo", "enqueue", confirm=False)
    operation = rig.core.sync.list_operations(rig.handle)[-1]
    history = rig.journal.sends(rig.scope)
    mutations = sum(q["query"].startswith("mutation") for q, _ in rig.provider.requests)
    marker = "OUTBOUND RACING FOREIGN PRIVATE CONTENT"
    withdrawal(rig, task, "assignee", marker)
    outcome = rig.outbound.run_once(rig.handle)
    assert outcome["state"] == "failed"
    assert_private(rig, task, marker, history)
    retained = next(o for o in rig.core.sync.list_operations(rig.handle) if o.id == operation.id)
    assert retained.fenced_by and retained.payload == operation.payload
    assert rig.current(task).desired_status == pending.desired_status
    assert sum(q["query"].startswith("mutation") for q, _ in rig.provider.requests) == mutations
    assert rig.outbound.run_once(rig.handle) is None


def test_outbound_readback_withdrawal_retains_uncertain_send_without_foreign_prose(rig):
    task = rig.task()
    rig.transition(task, "Todo", "enqueue", confirm=False)
    history = rig.journal.sends(rig.scope)
    marker = "AFTER SEND FOREIGN PRIVATE CONTENT"

    def after_response(request, _):
        if "mutation UpdateIssue" in request["query"]:
            withdrawal(rig, task, "assignee", marker)

    rig.provider.hook = after_response
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain" and result["error"] == "readback_race"
    sends = rig.journal.sends(rig.scope)
    assert sends[:-1] == history and sends[-1]["state"] == "uncertain"
    assert json.loads(sends[-1]["snapshot"])["title"] == task.title
    for path in (rig.journal.path, rig.db.path):
        assert marker.encode() not in Path(path).read_bytes()
    current = rig.current(task)
    assert current.human_hold and current.body == task.body and current.title == task.title
    latest = json.loads(rig.core.external.observations(rig.handle, task.id)[-1]["payload"])
    assert latest["assigned_to_owner"] is False and latest["title"] == task.title
    before = len(rig.provider.requests)
    assert rig.outbound.run_once(rig.handle) is None
    assert len(rig.provider.requests) == before


@pytest.mark.parametrize("missing", ["assignee", "delegate", "both"])
def test_missing_ownership_witness_keeps_only_safe_ingress_then_fences_fresh_foreign(rig, missing):
    task = rig.task()
    rig.receiver.binding = rig.client.binding
    marker = "MISSING WITNESS FOREIGN PRIVATE CONTENT"
    issue = withdrawal(rig, task, "assignee", marker)
    event = rig.provider.event(task.issue_id)
    event["data"].update(
        title=issue["title"],
        description=issue["description"],
        delegateId=issue["delegate"]["id"],
        private_text=marker,
    )
    event["updatedFrom"] = {"description": marker}
    if missing in {"assignee", "both"}:
        event["data"].pop("assigneeId")
    if missing in {"delegate", "both"}:
        event["data"].pop("delegateId")
        # Delegate-only omission must not smuggle the current foreign content
        # behind an earlier same-owner assignment claim.
        event["data"]["assigneeId"] = rig.scope.principal
    if missing == "both":
        event["data"].pop("assigneeId", None)
    headers, body = rig.signed(event)
    assert rig.receiver.receive("POST", headers, body).status == 200
    for path in (rig.journal.path, rig.db.path):
        assert marker.encode() not in Path(path).read_bytes()
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    assert_private(rig, task, marker, rig.journal.sends(rig.scope))
    if missing == "both":
        assert rig.receiver.receive("POST", headers, body).status == 200
        changed = copy.deepcopy(event)
        changed["data"]["description"] += " CHANGED"
        retry_headers, retry_body = rig.signed(changed, dict(headers)["Linear-Delivery"])
        assert rig.receiver.receive("POST", retry_headers, retry_body).status == 409


@pytest.mark.parametrize("operation", ["create", "update"])
def test_changed_assignee_mutation_response_retains_only_minimal_uncertain_identity(rig, operation):
    task = rig.task(confirmed=operation != "create")
    if operation == "update":
        rig.transition(task, "Todo", "enqueue", confirm=False)
    marker = "RETURNED RESPONSE FOREIGN PRIVATE CONTENT"
    returned = []

    def response(request, result):
        name = "issueCreate" if operation == "create" else "issueUpdate"
        if name not in result:
            return
        issue = result[name]["issue"]
        returned.append(issue["id"])
        issue.update(title=marker, description=marker, assignee={"id": uid(903)})

    rig.provider.hook = response
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain"
    for path in (rig.journal.path, rig.db.path):
        assert marker.encode() not in Path(path).read_bytes()
    send = rig.journal.sends(rig.scope)[-1]
    assert send["snapshot"] is None and send["state"] == "uncertain"
    with rig.journal.transaction() as db:
        rows = db.execute(
            "SELECT detail FROM history WHERE subject=? AND stage='mutation_scope_returned'",
            (send["id"],),
        ).fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["id"] == returned[0]
    current = rig.current(task)
    assert current.title == task.title and current.body == task.body
    before = len(rig.provider.requests)
    assert rig.outbound.run_once(rig.handle) is None
    assert len(rig.provider.requests) == before
