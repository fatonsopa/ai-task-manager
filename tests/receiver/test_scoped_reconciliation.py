"""Declared local provider recovery through the installed receiver composition."""

import json
import threading
import time

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import ValidationError
from tests.linear.helpers import STATES, uid

from .helpers import ROUTE, Fixture, request, wait_for
from .test_authority_boundary import revoke


@pytest.fixture
def fixture(tmp_path):
    f = Fixture(tmp_path)
    try:
        yield f
    finally:
        f.close()


def test_sr01_startup_recovers_missed_event_without_inventing_actor(fixture):
    fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    receiver.start()
    try:
        wait_for(
            lambda: service.core.get_task(service.handle, fixture.task.id).human_hold, seconds=1
        )
        task = service.core.get_task(service.handle, fixture.task.id)
        assert task.observed_status == "Backlog" and task.decision_revision == 0
        observations = service.core.external.observations(service.handle, task.id)
        assert (
            len(observations) == 1
            and json.loads(observations[0]["payload"])["actor_authorized"] is False
        )
        assert service.inbound.journal.deliveries(service.scope) == []
    finally:
        result = receiver.stop()
        (fixture.root / "reconciliation-cleanup.json").write_text(json.dumps(result))
        assert result["stopped"]


def test_sr02_periodic_same_owner_and_duplicate_are_once_only(fixture):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    receiver.start()
    try:
        wait_for(lambda: receiver.next_reconciliation > 0)
        fixture.provider.edit(fixture.task.issue_id, title="Changed remotely")
        receiver.next_reconciliation = 0
        receiver.reconciliation_wake.set()
        wait_for(lambda: service.core.get_task(service.handle, fixture.task.id).human_hold)
        before = service.core.get_task(service.handle, fixture.task.id)
        receiver.next_reconciliation = 0
        receiver.reconciliation_wake.set()
        wait_for(lambda: receiver.next_reconciliation > 0)
        assert service.core.get_task(service.handle, fixture.task.id).version == before.version
        assert len(service.core.external.observations(service.handle, before.id)) == 1
    finally:
        assert receiver.stop()["stopped"]


def test_sr03_revocation_during_recovery_read_has_no_core_effect(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    fixture.provider.edit(fixture.task.issue_id, title="Missed change")
    original = service.inbound.client.read_issue

    def read(issue):
        result = original(issue)
        revoke(receiver)
        return result

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    before = service.core.get_task(service.handle, fixture.task.id)
    receiver.start()
    try:
        wait_for(receiver.closing.is_set)
        assert service.core.get_task(service.handle, fixture.task.id) == before
        assert service.core.external.observations(service.handle, before.id) == []
        assert receiver.failed == "receiver_reconciliation_failed"
    finally:
        assert receiver.stop()["stopped"]


def test_sr04_slow_recovery_does_not_block_http_ack_and_yields(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    entered, release = threading.Event(), threading.Event()
    original = service.inbound.client.read_issue

    def read(issue):
        entered.set()
        assert release.wait(3)
        return original(issue)

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    receiver.start()
    try:
        assert entered.wait(2)
        headers, body = fixture.event()
        start = time.monotonic()
        assert request(receiver.http.address, ROUTE, headers, body)[0] == 200
        assert time.monotonic() - start < 1
        assert len(service.inbound.journal.deliveries(service.scope)) == 1
    finally:
        release.set()
        assert receiver.stop()["stopped"]


def test_sr05_rate_limit_backoff_cannot_be_bypassed_by_wake(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    calls = []

    def read(issue):
        calls.append(issue)
        raise LinearError("rate_limited", retry_after=600)

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    receiver.start()
    try:
        wait_for(lambda: receiver.provider_not_before > time.monotonic() + 590)
        for _ in range(4):
            receiver.wake.set()
            receiver.reconciliation_wake.set()
            time.sleep(0.03)
        assert calls == [fixture.task.issue_id] and not receiver.closing.is_set()
    finally:
        assert receiver.stop()["stopped"]


def test_sr06_registered_only_and_bounded_round_robin(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    fixture.provider.issues[uid(998)] = {
        **fixture.provider.issues[fixture.task.issue_id],
        "id": uid(998),
        "title": "Foreign",
    }
    reads = []
    original = service.inbound.client.read_issue

    def read(issue):
        reads.append(issue)
        return original(issue)

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    receiver._reconcile()
    assert reads == [fixture.task.issue_id]
    assert receiver.reconciliation_after == fixture.task.id
    assert len(service.core.list_tasks(service.handle)) == 1


def test_sr07_restart_recovers_new_gap_without_old_event_replay(fixture):
    first = InstalledReceiver(fixture.load(), fixture.config)
    first.start()
    wait_for(lambda: first.next_reconciliation > 0)
    assert first.stop()["stopped"]
    fixture.provider.edit(fixture.task.issue_id, title="Missed while stopped")
    service = fixture.load()
    second = InstalledReceiver(service, fixture.config)
    second.start()
    try:
        wait_for(lambda: service.core.get_task(service.handle, fixture.task.id).human_hold)
        assert service.inbound.journal.deliveries(service.scope) == []
    finally:
        assert second.stop()["stopped"]


@pytest.mark.parametrize("interval", [0, 119, 901, True, 120.0])
def test_sr08_trusted_interval_bounds_refuse(fixture, interval):
    write_private(
        fixture.receiver, {**fixture.values, "reconciliation_seconds": interval}, replace=True
    )
    with pytest.raises(ValidationError, match="Reconciliation interval"):
        InstalledReceiver(fixture.load(), fixture.config)


def test_sr09_read_occurs_without_core_writer_and_limit_is_fair(fixture, monkeypatch):
    import sqlite3

    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    original = service.inbound.client.read_issue
    reads = []

    def read(issue):
        with sqlite3.connect(service.core.context.db.path, timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        reads.append(issue)
        return original(issue)

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    receiver.wake.set()  # Continuous incoming work must not starve the first safety read.
    receiver._reconcile()
    assert reads == [fixture.task.issue_id]


def test_sr10_assignee_removal_fences_without_actor(fixture):
    fixture.provider.edit(fixture.task.issue_id, assignee=None)
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    receiver.start()
    try:
        wait_for(lambda: service.core.get_task(service.handle, fixture.task.id).human_hold)
        task = service.core.get_task(service.handle, fixture.task.id)
        assert task.decision_revision == 0
        payload = json.loads(
            service.core.external.observations(service.handle, task.id)[0]["payload"]
        )
        assert payload["assigned_to_owner"] is False and payload["actor_authorized"] is False
    finally:
        assert receiver.stop()["stopped"]


def test_sr11_delegate_loss_refuses_instead_of_matching(fixture):
    fixture.provider.edit(fixture.task.issue_id, delegate=None)
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    receiver.start()
    try:
        wait_for(receiver.closing.is_set)
        assert receiver.failed == "receiver_reconciliation_failed"
        assert service.core.get_task(service.handle, fixture.task.id).observed_status == "Backlog"
    finally:
        assert receiver.stop()["stopped"]


def test_sr12_stopped_owner_during_blocked_read_is_truthful(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    entered = threading.Event()
    release = threading.Event()
    original = service.inbound.client.read_issue

    def read(issue):
        entered.set()
        assert release.wait(3)
        return original(issue)

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    receiver.start()
    assert entered.wait(2)
    try:
        result = receiver.stop(timeout=0.01)
        assert not result["stopped"] and not result["ownership_released"]
    finally:
        release.set()
        assert receiver.stop()["stopped"]


def test_sr13_blocked_periodic_read_does_not_delay_webhook_application(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    entered, release = threading.Event(), threading.Event()
    original = service.inbound.client.read_issue

    def read(issue):
        snapshot = original(issue)
        if threading.current_thread().name == "linear-reconciliation":
            entered.set()
            assert release.wait(4)
        return snapshot

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    receiver.start()
    try:
        assert entered.wait(2)
        fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        headers, body = fixture.event()
        start = time.monotonic()
        assert request(receiver.http.address, ROUTE, headers, body)[0] == 200
        wait_for(
            lambda: (
                service.core.get_task(service.handle, fixture.task.id).observed_status == "Todo"
            ),
            seconds=2,
        )
        elapsed = time.monotonic() - start
        assert not release.is_set() and elapsed < 2
        (fixture.root / "blocked-recovery-application-timing.json").write_text(
            json.dumps({"elapsed_seconds": elapsed, "periodic_read_still_blocked": True})
        )
        release.set()
        wait_for(lambda: receiver.next_reconciliation > 0)
        assert service.core.get_task(service.handle, fixture.task.id).observed_status == "Todo"
    finally:
        release.set()
        result = receiver.stop()
        assert result["stopped"] and result["reconciliation_stopped"]


def test_sr14_more_than_one_slice_is_bounded_and_fair(fixture, monkeypatch):
    for _ in range(9):
        fixture.rig.task()
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    tasks = service.core.list_tasks(service.handle)
    assert len(tasks) == 10
    reads = []
    original = service.inbound.client.read_issue

    def read(issue):
        reads.append(issue)
        return original(issue)

    monkeypatch.setattr(service.inbound.client, "read_issue", read)
    receiver._reconcile()
    assert len(reads) == 8
    receiver._reconcile()
    assert len(reads) == 16
    assert set(reads) == {task.issue_id for task in tasks}
