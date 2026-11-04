"""R04/R14/R37: bounded reordered receipts and ownership loss at pending echoes."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.application.service.projection import quarantined
from tests.linear.helpers import STATES, uid

from .test_confirmation_wake import fixture as fixture


def uncertain(fixture, monkeypatch, *, known=True):
    service = fixture.load()
    original = fixture.event()
    task = fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)
    client = service.inbound.client
    update, read = client.update_issue, client.read_issue

    def lost(*args):
        raise LinearError("authored_response_loss", uncertain=True)

    def update_then_fail(*args):
        result = update(*args)
        if not known:
            lost()
        monkeypatch.setattr(client, "read_issue", lost)
        return result

    monkeypatch.setattr(client, "update_issue", update_then_fail)
    result = OutboundDrain(service.core, client, service.inbound.journal).run_once(
        service.handle, task_id=task.id
    )
    assert result["state"] == "uncertain"
    monkeypatch.setattr(client, "update_issue", update)
    monkeypatch.setattr(client, "read_issue", read)
    return service, task, original, result["operation_id"]


def accept(service, fixture, event):
    # Use the installed receiver's unchanged authenticated intake without a
    # background drain; this makes each provider read and selection finite.
    receiver = InstalledReceiver(service, fixture.config)
    headers, body = event
    assert receiver.adapter.receive("POST", headers, body).status == 200
    delivery = dict(headers)["Linear-Delivery"]
    return next(r for r in rows(service) if r["delivery_id"] == delivery)


def rows(service):
    return service.inbound.journal.deliveries(service.scope)


def run(service):
    return service.inbound.run_once(service.handle)


@pytest.mark.parametrize("order", ["newer_first", "older_first", "offset_older"])
def test_wake_p1_reordered_known_echo_progresses_without_provider_spin(fixture, monkeypatch, order):
    other = fixture.rig.task()
    service, task, older, operation_id = uncertain(fixture, monkeypatch)
    if order == "offset_older":
        payload = json.loads(older[1])
        stamp = datetime.fromisoformat(payload["data"]["updatedAt"].replace("Z", "+00:00"))
        payload["data"]["updatedAt"] = stamp.astimezone(timezone(timedelta(hours=2))).isoformat()
        older = fixture.rig.signed(payload)
    current = fixture.event()
    if order == "older_first":
        delayed = accept(service, fixture, older)
        original = accept(service, fixture, current)
    else:
        original = accept(service, fixture, current)
        assert run(service)["state"] == "pending"
        delayed = accept(service, fixture, older)
    fixture.provider.edit(other.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    payload = fixture.provider.event(other.issue_id)
    payload["webhookTimestamp"] = json.loads(current[1])["webhookTimestamp"]
    unrelated = accept(service, fixture, fixture.rig.signed(payload))
    assert run(service) == {
        "state": "conflict",
        "error": "event_readback_revision_conflict",
        "task_id": task.id,
    }
    if order == "older_first":
        assert run(service)["state"] == "pending"
    assert run(service)["state"] == "applied"
    assert next(r for r in rows(service) if r["id"] == delayed["id"])["state"] == "conflict"
    assert next(r for r in rows(service) if r["id"] == unrelated["id"])["state"] == "applied"
    before = len(fixture.provider.requests)
    for _ in range(16):
        assert run(service) is None
    assert len(fixture.provider.requests) == before
    assert not service.core.external.observations(service.handle, task.id)
    operation = next(
        o for o in service.core.sync.list_operations(service.handle) if o.id == operation_id
    )
    assert not operation.fenced_by and operation.state == "uncertain"
    Reconciliation(service.core, service.inbound.client, service.inbound.journal).recover_outbound(
        service.handle, operation_id
    )
    assert run(service) == {"state": "applied", "classification": "outbound_echo"}
    final = next(r for r in rows(service) if r["id"] == original["id"])
    assert final["payload"] == original["payload"] and final["raw_digest"] == original["raw_digest"]
    assert final["state"] == "applied" and run(service) is None
    assert sum("mutation UpdateIssue" in q["query"] for q, _ in fixture.provider.requests) == 1


@pytest.mark.parametrize("assignee", [None, {"id": uid(900)}])
@pytest.mark.parametrize("known", [True, False])
def test_wake_p2_first_uncertain_event_fences_removed_assignment(
    fixture, monkeypatch, assignee, known
):
    service, task, _, operation_id = uncertain(fixture, monkeypatch, known=known)
    assert rows(service) == []
    fixture.provider.edit(task.issue_id, assignee=assignee)
    original = accept(service, fixture, fixture.event())
    run(service)
    current = service.core.get_task(service.handle, task.id)
    assert current.human_hold and quarantined(service, current)
    operation = next(
        o for o in service.core.sync.list_operations(service.handle) if o.id == operation_id
    )
    assert operation.fenced_by
    assert not json.loads(rows(service)[0]["checkpoint"]).get("awaiting_outbound")
    assert rows(service)[0]["id"] == original["id"] and len(rows(service)) == 1
    before = len(fixture.provider.requests)
    assert run(service) is None
    assert len(fixture.provider.requests) == before
    assert sum("mutation UpdateIssue" in q["query"] for q, _ in fixture.provider.requests) == 1


@pytest.mark.parametrize("first_event", [True, False])
def test_wake_same_status_human_revision_is_not_hidden_as_uncertain_echo(
    fixture, monkeypatch, first_event
):
    service, task, _, operation_id = uncertain(fixture, monkeypatch)
    if not first_event:
        accept(service, fixture, fixture.event())
        assert run(service)["state"] == "pending"
    fixture.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    human = accept(service, fixture, fixture.event())
    for _ in range(3):
        run(service)
    observations = service.core.external.observations(service.handle, task.id)
    assert any(json.loads(o["payload"])["actor_authorized"] for o in observations)
    operation = next(
        o for o in service.core.sync.list_operations(service.handle) if o.id == operation_id
    )
    assert operation.fenced_by
    assert service.core.get_task(service.handle, task.id).decision_revision == 1
    assert next(r for r in rows(service) if r["id"] == human["id"])["state"] == "applied"
    before = len(fixture.provider.requests)
    assert run(service) is None and len(fixture.provider.requests) == before


def test_wake_unknown_response_does_not_prove_reordered_echo(fixture, monkeypatch):
    service, task, older, operation_id = uncertain(fixture, monkeypatch, known=False)
    accept(service, fixture, older)
    assert run(service)["state"] == "conflict"
    assert service.core.get_task(service.handle, task.id).human_hold
    operation = next(
        o for o in service.core.sync.list_operations(service.handle) if o.id == operation_id
    )
    assert operation.fenced_by
    with pytest.raises(LinearError, match="operation_not_recoverable"):
        Reconciliation(
            service.core, service.inbound.client, service.inbound.journal
        ).recover_outbound(service.handle, operation_id)


def test_wake_equivalent_revision_spellings_do_not_rewake_parked_receipts(fixture, monkeypatch):
    service, _, _, operation_id = uncertain(fixture, monkeypatch)
    event = fixture.event()
    accept(service, fixture, event)
    assert run(service)["state"] == "pending"
    payload = json.loads(event[1])
    stamp = datetime.fromisoformat(payload["data"]["updatedAt"].replace("Z", "+00:00"))
    payload["data"]["updatedAt"] = stamp.astimezone(timezone(timedelta(hours=2))).isoformat()
    accept(service, fixture, fixture.rig.signed(payload))
    assert run(service)["state"] == "pending"
    before = len(fixture.provider.requests)
    for _ in range(16):
        assert run(service) is None
    assert len(fixture.provider.requests) == before
    Reconciliation(service.core, service.inbound.client, service.inbound.journal).recover_outbound(
        service.handle, operation_id
    )
    assert run(service)["classification"] == "outbound_echo"
    assert run(service)["classification"] == "outbound_echo"
    assert run(service) is None


def test_wake_newer_event_with_lagging_readback_cannot_repark_old_receipt_forever(
    fixture, monkeypatch
):
    service, task, _, operation_id = uncertain(fixture, monkeypatch)
    accept(service, fixture, fixture.event())
    assert run(service)["state"] == "pending"
    old = service.inbound.client.read_issue(task.issue_id)
    fixture.provider.edit(task.issue_id, state={"id": STATES["Backlog"], "name": "Backlog"})
    newer = accept(service, fixture, fixture.event())
    original_read = service.inbound.client.read_issue
    calls = []

    def lagging(issue_id):
        calls.append(issue_id)
        return old

    monkeypatch.setattr(service.inbound.client, "read_issue", lagging)
    assert run(service)["state"] == "conflict"
    assert next(r for r in rows(service) if r["id"] == newer["id"])["state"] == "conflict"
    assert len(calls) == 1
    operation = next(
        o for o in service.core.sync.list_operations(service.handle) if o.id == operation_id
    )
    assert operation.fenced_by
    monkeypatch.setattr(service.inbound.client, "read_issue", original_read)
    assert run(service)["state"] == "conflict"
    before = len(fixture.provider.requests)
    for _ in range(16):
        assert run(service) is None
    assert len(fixture.provider.requests) == before
