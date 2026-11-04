"""Registered recovery preserves exact pending sends and fences other revisions."""

import json
from dataclasses import replace

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.domains.work.errors import AccessDenied
from tests.linear.helpers import uid

from .test_authority_boundary import revoke
from .test_scoped_reconciliation import fixture as fixture
from .test_wake_correction import accept, run, uncertain


def operation(service, operation_id):
    return next(
        o for o in service.core.sync.list_operations(service.handle) if o.id == operation_id
    )


@pytest.mark.parametrize("boundary", ["mutation_returned", "readback"])
@pytest.mark.parametrize("webhook", [False, True])
def test_recovery_between_mutation_response_and_confirmation(
    fixture, monkeypatch, webhook, boundary
):
    task = fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    finish = fixture.rig.journal.finish_send
    observed = []

    def after_response(send_id, state, *args, **kwargs):
        result = finish(send_id, state, *args, **kwargs)
        if state == boundary:
            before = service.core.get_task(service.handle, task.id)
            if webhook:
                accept(service, fixture, fixture.event())
                assert run(service)["state"] == "pending"
            receiver._reconcile()
            after = service.core.get_task(service.handle, task.id)
            observed.append((before, after))
        return result

    monkeypatch.setattr(fixture.rig.journal, "finish_send", after_response)
    try:
        result = fixture.rig.outbound.run_once(fixture.rig.handle)
        assert result["state"] == "confirmed"
        assert len(observed) == 1 and observed[0][0] == observed[0][1]
        assert not operation(service, result["operation_id"]).fenced_by
        final = service.core.get_task(service.handle, task.id)
        assert final.observed_status == "Todo" and not final.human_hold
        if webhook:
            assert run(service) == {"state": "applied", "classification": "outbound_echo"}
            assert run(service) is None
        receiver._reconcile()
        assert service.core.get_task(service.handle, task.id) == final
        assert service.core.external.observations(service.handle, task.id) == []
        assert sum("mutation UpdateIssue" in q["query"] for q, _ in fixture.provider.requests) == 1
    finally:
        assert receiver.stop()["stopped"]


@pytest.mark.parametrize("known", [False, True])
def test_recovery_keeps_exact_uncertain_response_distinct_from_unknown(fixture, monkeypatch, known):
    service, task, _, operation_id = uncertain(fixture, monkeypatch, known=known)
    receiver = InstalledReceiver(service, fixture.config)
    try:
        before = service.core.get_task(service.handle, task.id)
        receiver._reconcile()
        current = service.core.get_task(service.handle, task.id)
        pending = operation(service, operation_id)
        assert pending.state == "uncertain"
        if known:
            assert current == before and not pending.fenced_by
            assert service.core.external.observations(service.handle, task.id) == []
            Reconciliation(
                service.core, service.inbound.client, service.inbound.journal
            ).recover_outbound(service.handle, operation_id)
            assert service.core.get_task(service.handle, task.id).observed_status == "Todo"
        else:
            assert current.human_hold and pending.fenced_by
            assert current.observed_status == "Backlog"
    finally:
        assert receiver.stop()["stopped"]


@pytest.mark.parametrize("change", [{}, {"title": "Human revision"}, {"assignee": None}])
def test_recovery_fences_newer_snapshot_even_with_same_pending_status(fixture, monkeypatch, change):
    service, task, _, operation_id = uncertain(fixture, monkeypatch)
    fixture.provider.edit(task.issue_id, **change)
    receiver = InstalledReceiver(service, fixture.config)
    try:
        receiver._reconcile()
        current = service.core.get_task(service.handle, task.id)
        assert current.human_hold and operation(service, operation_id).fenced_by
        observations = service.core.external.observations(service.handle, task.id)
        assert len(observations) == 1
        assert json.loads(observations[0]["payload"])["actor_authorized"] is False
    finally:
        assert receiver.stop()["stopped"]


@pytest.mark.parametrize("witness", ["foreign_scope", "old_attempt", "other_operation"])
def test_recovery_does_not_borrow_an_exact_response(fixture, monkeypatch, witness):
    service, task, _, operation_id = uncertain(fixture, monkeypatch, known=False)
    pending = operation(service, operation_id)
    journal = service.inbound.journal
    snapshot = service.inbound.client.read_issue(task.issue_id)
    scope = (
        replace(service.scope, logical_session="foreign")
        if witness == "foreign_scope"
        else service.scope
    )
    recorded = (
        replace(pending, attempt_count=pending.attempt_count - 1)
        if witness == "old_attempt"
        else replace(pending, id=uid(912))
        if witness == "other_operation"
        else pending
    )
    original = next(s for s in journal.sends(service.scope) if s["operation_id"] == pending.id)
    send = journal.begin_send(scope, recorded, connection_digest=original["connection_digest"])
    journal.finish_send(send, "mutation_returned", snapshot)
    receiver = InstalledReceiver(service, fixture.config)
    try:
        receiver._reconcile()
        assert service.core.get_task(service.handle, task.id).human_hold
        assert operation(service, operation_id).fenced_by
    finally:
        assert receiver.stop()["stopped"]


def test_recovery_rechecks_authority_after_pending_echo_lookup(fixture, monkeypatch):
    service, task, _, operation_id = uncertain(fixture, monkeypatch)
    receiver = InstalledReceiver(service, fixture.config)
    journal = service.inbound.journal
    sends = journal.sends
    before = service.core.get_task(service.handle, task.id)

    def revoked(scope):
        result = sends(scope)
        revoke(receiver)
        return result

    monkeypatch.setattr(journal, "sends", revoked)
    try:
        with pytest.raises(AccessDenied):
            receiver._reconcile()
        assert service.core.get_task(service.handle, task.id) == before
        assert not operation(service, operation_id).fenced_by
    finally:
        assert receiver.stop()["stopped"]


@pytest.mark.parametrize("field", ["delegate", "project"])
def test_recovery_preserves_scope_and_delegate_checks_with_pending_echo(
    fixture, monkeypatch, field
):
    service, task, _, operation_id = uncertain(fixture, monkeypatch)
    fixture.provider.edit(task.issue_id, **{field: {"id": uid(913)}})
    receiver = InstalledReceiver(service, fixture.config)
    try:
        if field == "delegate":
            with pytest.raises(LinearError):
                receiver._reconcile()
        else:
            receiver._reconcile()
            assert service.core.get_task(service.handle, task.id).human_hold
            assert operation(service, operation_id).fenced_by
    finally:
        assert receiver.stop()["stopped"]
