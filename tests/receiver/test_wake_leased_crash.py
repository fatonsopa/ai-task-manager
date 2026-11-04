"""WAKE-P2-3: retained response and lease boundaries survive an actual child exit."""

import json
from dataclasses import replace

import pytest

from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from tests.linear.helpers import STATES

from .helpers import wait_for
from .test_confirmation_wake import Trial
from .test_confirmation_wake import fixture as fixture
from .test_wake_correction import accept, rows, run


def crashed(fixture, monkeypatch, *, known=True):
    trial = Trial(fixture, monkeypatch, mode="crash_response" if known else "crash_unknown")
    assert trial.receiver.stop()["stopped"]
    trial.release.set()
    output, error = trial.child.communicate(timeout=10)
    assert trial.child.returncode == (71 if known else 72) and not output and not error
    operation = trial.service.core.sync.list_operations(trial.service.handle)[-1]
    assert operation.state == "leased" and not operation.fenced_by
    return trial, operation


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_wake_known_leased_crash_cannot_hide_newer_human(fixture, monkeypatch, expired, restart):
    trial, operation = crashed(fixture, monkeypatch)
    try:
        service = trial.service
        accept(service, fixture, fixture.event())
        assert run(service)["state"] == "pending"
        fixture.provider.edit(trial.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        human = accept(service, fixture, fixture.event())
        if restart:
            service = fixture.load()
        if expired:
            monkeypatch.setattr(service.inbound, "clock", lambda: operation.lease_until + 1)
        if restart:
            trial.service = service
            trial.receiver = InstalledReceiver(service, fixture.config)
            trial.receiver.start()
            wait_for(lambda: all(r["state"] in {"applied", "conflict"} for r in rows(service)))
        else:
            assert run(service)["state"] == "applied"
        task = service.core.get_task(service.handle, trial.task.id)
        assert task.decision_revision == 1
        observations = service.core.external.observations(service.handle, task.id)
        assert any(json.loads(o["payload"])["actor_authorized"] for o in observations)
        current = service.core.sync.list_operations(service.handle)[-1]
        assert current.id == operation.id and current.fenced_by
        assert next(r for r in rows(service) if r["id"] == human["id"])["state"] == "applied"
        run(service)  # Settle the superseded original receipt through its existing conflict path.
        before = len(fixture.provider.requests)
        for _ in range(16):
            assert run(service) is None
        assert len(fixture.provider.requests) == before
        assert sum("mutation UpdateIssue" in q["query"] for q, _ in fixture.provider.requests) == 1
    finally:
        trial.close()


@pytest.mark.parametrize("witness", ["missing", "foreign_scope", "old_attempt"])
def test_wake_unknown_leased_crash_has_finite_local_recovery(fixture, monkeypatch, witness):
    trial, operation = crashed(fixture, monkeypatch, known=False)
    try:
        service = trial.service
        if witness != "missing":
            snapshot = service.inbound.client.read_issue(trial.task.issue_id)
            scope = (
                replace(service.scope, logical_session="foreign")
                if witness == "foreign_scope"
                else service.scope
            )
            recorded = (
                replace(operation, attempt_count=operation.attempt_count - 1)
                if witness == "old_attempt"
                else operation
            )
            original_send = next(
                s
                for s in service.inbound.journal.sends(service.scope)
                if s["operation_id"] == operation.id and s["attempt"] == operation.attempt_count
            )
            send = service.inbound.journal.begin_send(
                scope, recorded, connection_digest=original_send["connection_digest"]
            )
            service.inbound.journal.finish_send(send, "mutation_returned", snapshot)
        receipt = accept(service, fixture, fixture.event())
        assert run(service)["state"] == "pending"
        before = len(fixture.provider.requests)
        for _ in range(8):
            assert run(service) is None
        assert len(fixture.provider.requests) == before
        service = fixture.load()
        monkeypatch.setattr(service.inbound, "clock", lambda: operation.lease_until + 1)
        result = run(service)
        assert result["state"] == "conflict" and result["error"] == "outbound_response_unknown"
        final = rows(service)[0]
        assert final["id"] == receipt["id"] and final["payload"] == receipt["payload"]
        assert final["state"] == "conflict"
        current = service.core.sync.list_operations(service.handle)[-1]
        assert current.state == "leased" and current.attempt_count == operation.attempt_count
        assert not service.core.external.observations(service.handle, trial.task.id)
        before = len(fixture.provider.requests)
        for _ in range(16):
            assert run(service) is None
        assert len(fixture.provider.requests) == before
        assert sum("mutation UpdateIssue" in q["query"] for q, _ in fixture.provider.requests) == 1
    finally:
        trial.close()


def test_wake_known_exact_expired_response_still_needs_explicit_confirmation(fixture, monkeypatch):
    trial, operation = crashed(fixture, monkeypatch)
    try:
        service = trial.service
        receipt = accept(service, fixture, fixture.event())
        assert run(service)["state"] == "pending"
        monkeypatch.setattr(service.inbound, "clock", lambda: operation.lease_until + 1)
        before = len(fixture.provider.requests)
        for _ in range(16):
            assert run(service) is None
        assert len(fixture.provider.requests) == before
        Reconciliation(
            service.core, service.inbound.client, service.inbound.journal
        ).recover_outbound(service.handle, operation.id)
        assert run(service)["classification"] == "outbound_echo"
        assert rows(service)[0]["id"] == receipt["id"]
        assert service.core.get_task(service.handle, trial.task.id).decision_revision == 0
    finally:
        trial.close()


@pytest.mark.parametrize("outcome", ["leased", "uncertain"])
def test_wake_delayed_response_disproves_already_parked_human_after_restart(
    fixture, monkeypatch, outcome
):
    trial, operation = crashed(fixture, monkeypatch, known=False)
    try:
        service = trial.service
        response = service.inbound.client.read_issue(trial.task.issue_id)
        accept(service, fixture, fixture.event())
        assert run(service)["state"] == "pending"
        fixture.provider.edit(trial.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        human = accept(service, fixture, fixture.event())
        assert run(service)["state"] == "pending"
        if outcome == "uncertain":
            service.core.sync.record_outcome(
                service.handle,
                operation.id,
                operation.lease_owner,
                operation.attempt_count,
                "uncertain",
                "authored delayed response record",
            )
        # Authored journal ordering: persist the captured R2 response after R3
        # parked. The separate child really exited; no child resumption is claimed.
        send = next(
            s
            for s in service.inbound.journal.sends(service.scope)
            if s["operation_id"] == operation.id
        )
        service.inbound.journal.finish_send(send["id"], "mutation_returned", response)
        service = fixture.load()
        trial.service = service
        trial.receiver = InstalledReceiver(service, fixture.config)
        trial.receiver.start()
        wait_for(lambda: all(r["state"] in {"applied", "conflict"} for r in rows(service)))
        current = service.core.get_task(service.handle, trial.task.id)
        assert current.decision_revision == 1
        assert service.core.sync.list_operations(service.handle)[-1].fenced_by
        assert next(r for r in rows(service) if r["id"] == human["id"])["state"] == "applied"
        before = len(fixture.provider.requests)
        for _ in range(16):
            assert run(service) is None
        assert len(fixture.provider.requests) == before
    finally:
        trial.close()
