"""Bounded live-request deferral and conservative recovery after ownership loss."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ai_task_manager.application.linear_sync.inflight import owned_request, request_in_flight
from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.domains.work.errors import AccessDenied
from tests.linear.helpers import STATES, uid

from .test_authority_boundary import revoke
from .test_confirmation_wake import Trial
from .test_scoped_reconciliation import fixture as fixture


@pytest.mark.parametrize("boundary", ["before_effect", "after_effect", "older_read"])
def test_inflight_same_process_thread_defers_without_confirmation(fixture, monkeypatch, boundary):
    service = fixture.load()
    task = fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)
    receiver = InstalledReceiver(service, fixture.config)
    entered, release = threading.Event(), threading.Event()
    update = fixture.rig.client.update_issue

    def blocked(*args):
        snapshot = update(*args) if boundary == "after_effect" else None
        entered.set()
        assert release.wait(5)
        return snapshot or update(*args)

    if boundary == "older_read":
        read = service.inbound.client.read_issue
        monkeypatch.setattr(
            service.inbound.client,
            "read_issue",
            lambda issue: replace(read(issue), revision="2026-10-31T08:00:00.000000Z"),
        )

    monkeypatch.setattr(fixture.rig.client, "update_issue", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fixture.rig.outbound.run_once, fixture.rig.handle)
        try:
            assert entered.wait(3)
            receiver._reconcile()
            current = service.core.get_task(service.handle, task.id)
            assert current == task and not current.human_hold
            pending = service.core.sync.list_operations(service.handle)[-1]
            assert not pending.fenced_by and pending.state == "leased"
            if boundary == "after_effect":
                assert receiver.next_reconciliation < time.monotonic() + 31
        finally:
            release.set()
            assert receiver.stop()["stopped"]
        assert future.result(timeout=3)["state"] == "confirmed"


@pytest.mark.parametrize(
    "fault",
    ["expired", "dead", "human_title", "human_backlog", "assignee", "scope", "delegate", "revoked"],
)
def test_inflight_current_safety_boundaries(fixture, monkeypatch, fault):
    trial = Trial(fixture, monkeypatch)
    try:
        assert trial.receiver.stop()["stopped"]
        service = trial.service
        pending = service.core.sync.list_operations(service.handle)[-1]
        before = service.core.get_task(service.handle, trial.task.id)
        if fault == "expired":
            monkeypatch.setattr(service.core.context, "clock", lambda: pending.lease_until + 1)
        elif fault == "dead":
            trial.child.kill()
            trial.child.communicate(timeout=3)
        elif fault == "human_title":
            fixture.provider.edit(trial.task.issue_id, title="New human instruction")
        elif fault == "human_backlog":
            fixture.provider.edit(
                trial.task.issue_id, state={"id": STATES["Backlog"], "name": "Backlog"}
            )
        elif fault == "assignee":
            fixture.provider.edit(trial.task.issue_id, assignee=None)
        elif fault == "scope":
            fixture.provider.edit(trial.task.issue_id, project={"id": uid(922)})
        elif fault == "delegate":
            fixture.provider.edit(trial.task.issue_id, delegate=None)
        receiver = InstalledReceiver(service, fixture.config)
        try:
            if fault == "revoked":
                revoke(receiver)
                with pytest.raises(AccessDenied):
                    receiver._reconcile()
                assert service.core.get_task(service.handle, trial.task.id) == before
            elif fault == "delegate":
                from ai_task_manager.adapters.linear.errors import LinearError

                with pytest.raises(LinearError):
                    receiver._reconcile()
            else:
                receiver._reconcile()
                current = service.core.get_task(service.handle, trial.task.id)
                assert current.human_hold
                assert service.core.sync.list_operations(service.handle)[-1].fenced_by
        finally:
            assert receiver.stop()["stopped"]
    finally:
        trial.close()


@pytest.mark.parametrize("witness", ["missing", "unlocked", "old_attempt", "foreign_scope"])
def test_inflight_stale_or_foreign_ownership_never_defers(fixture, witness):
    task = fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)
    service = fixture.load()
    operation = service.core.sync.claim(service.handle, "authored-send-owner")
    journal = service.inbound.journal
    send = journal.begin_send(service.scope, operation)
    fixture.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    scope = (
        replace(service.scope, logical_session="foreign")
        if witness == "foreign_scope"
        else service.scope
    )
    other = (
        replace(operation, attempt_count=operation.attempt_count + 1)
        if witness == "old_attempt"
        else operation
    )
    receiver = InstalledReceiver(service, fixture.config)
    try:
        if witness == "missing":
            receiver._reconcile()
        elif witness == "unlocked":
            with owned_request(journal, scope, other, send):
                assert request_in_flight(journal, service.scope, operation)
            receiver._reconcile()
        else:
            with owned_request(journal, scope, other, send):
                assert not request_in_flight(journal, service.scope, operation)
                receiver._reconcile()
        assert service.core.get_task(service.handle, task.id).human_hold
        assert service.core.sync.list_operations(service.handle)[-1].fenced_by
    finally:
        assert receiver.stop()["stopped"]


@pytest.mark.parametrize("fault", ["replaced", "metadata", "hardlink"])
def test_inflight_owner_file_requires_exact_retained_identity(fixture, fault):
    from pathlib import Path

    from ai_task_manager.adapters.linear.errors import LinearError

    fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)
    service = fixture.load()
    operation = service.core.sync.claim(service.handle, "authored-send-owner")
    journal = service.inbound.journal
    send = journal.begin_send(service.scope, operation)
    expected = LinearError if fault in {"replaced", "hardlink"} else None
    try:
        with owned_request(journal, service.scope, operation, send):
            paths = list(Path(journal.path).parent.glob(".linear-request-*.lock"))
            path = next(p for p in paths if '"send":"' + send + '"' in p.read_text())
            assert request_in_flight(journal, service.scope, operation)
            if fault == "replaced":
                raw = path.read_bytes()
                path.unlink()
                path.write_bytes(raw)
                path.chmod(0o600)
                assert not request_in_flight(journal, service.scope, operation)
            elif fault == "metadata":
                path.write_text("{}")
                assert not request_in_flight(journal, service.scope, operation)
            else:
                path.with_suffix(".alias").hardlink_to(path)
                with pytest.raises(LinearError):
                    request_in_flight(journal, service.scope, operation)
    except LinearError:
        assert expected is LinearError
    else:
        assert expected is None


def test_response_publication_between_echo_lookup_and_owner_probe(fixture, monkeypatch):
    from ai_task_manager.application.linear_sync import reconcile

    service = fixture.load()
    task = fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)
    receiver = InstalledReceiver(service, fixture.config)
    entered, return_response = threading.Event(), threading.Event()
    recorded, finish_response = threading.Event(), threading.Event()
    update, finish = fixture.rig.client.update_issue, fixture.rig.journal.finish_send
    probe = reconcile.request_in_flight

    def blocked_update(*args):
        snapshot = update(*args)
        entered.set()
        assert return_response.wait(5)
        return snapshot

    def blocked_finish(send_id, state, *args, **kwargs):
        result = finish(send_id, state, *args, **kwargs)
        if state == "mutation_returned":
            recorded.set()
            assert finish_response.wait(5)
        return result

    def after_lookup(*args):
        return_response.set()
        assert recorded.wait(3)
        return probe(*args)

    monkeypatch.setattr(fixture.rig.client, "update_issue", blocked_update)
    monkeypatch.setattr(fixture.rig.journal, "finish_send", blocked_finish)
    monkeypatch.setattr(reconcile, "request_in_flight", after_lookup)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fixture.rig.outbound.run_once, fixture.rig.handle)
        try:
            assert entered.wait(3)
            receiver._reconcile()
            assert service.core.get_task(service.handle, task.id) == task
            assert not service.core.sync.list_operations(service.handle)[-1].fenced_by
        finally:
            return_response.set()
            finish_response.set()
            assert receiver.stop()["stopped"]
        assert future.result(timeout=3)["state"] == "confirmed"


def test_deferred_task_is_revisited_without_losing_round_robin_cursor(fixture, monkeypatch):
    for _ in range(8):
        fixture.rig.task()
    service = fixture.load()
    task = fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)
    receiver = InstalledReceiver(service, fixture.config)
    receiver.reconciliation_after = task.id[:-1]
    entered, release = threading.Event(), threading.Event()
    update = fixture.rig.client.update_issue
    reads = []
    read = service.inbound.client.read_issue

    def blocked(*args):
        snapshot = update(*args)
        entered.set()
        assert release.wait(5)
        return snapshot

    def counted(issue):
        reads.append(issue)
        return read(issue)

    monkeypatch.setattr(fixture.rig.client, "update_issue", blocked)
    monkeypatch.setattr(service.inbound.client, "read_issue", counted)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fixture.rig.outbound.run_once, fixture.rig.handle)
        try:
            assert entered.wait(3)
            receiver._reconcile()
            assert reads[0] == task.issue_id and len(reads) == 8
            cursor = receiver.reconciliation_after
            assert cursor != task.id and task.id in receiver.reconciliation_pending
            receiver.reconciliation_pending[task.id] = time.monotonic() - 1
            reads.clear()
            receiver._reconcile()
            assert reads == [task.issue_id]
            assert receiver.reconciliation_after == cursor
            assert service.core.get_task(service.handle, task.id) == task
        finally:
            release.set()
            assert receiver.stop()["stopped"]
        assert future.result(timeout=3)["state"] == "confirmed"
