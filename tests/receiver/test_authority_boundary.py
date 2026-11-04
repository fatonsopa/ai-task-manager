"""Actual local readback ordering and signed enrollment, never live provider proof."""

import json
import threading
import time

import pytest

from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.config.files import digest, write_private
from ai_task_manager.domains.work.errors import AccessDenied
from tests.linear.helpers import STATES, uid

from .helpers import ROUTE, Fixture, request, wait_for


@pytest.fixture
def fixture(tmp_path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


def revoke(receiver):
    bind_record(
        receiver.observations.journal,
        "session-revoked:" + digest(receiver.service.grant_ref),
        {"scope": {}, "reason": "authored authority-boundary control"},
    )


def assert_recoverable(service):
    rows = service.inbound.journal.deliveries(service.scope)
    assert len(rows) == 1 and rows[0]["state"] == "received"
    assert rows[0]["owner"] is None and rows[0]["lease_until"] is None
    assert all(row["state"] != "applied" for row in service.core.sync.inbound(service.handle))
    assert service.core.session_binding(service.handle)  # Independent capability stays valid.


@pytest.mark.parametrize("branch", ["ordinary", "reordered", "echo"])
@pytest.mark.parametrize("fault", ["revoke", "expire", "config", "secret", "credential"])
def test_ra01_ra02_authority_change_during_readback_has_no_later_effect(
    fixture, monkeypatch, branch, fault
):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    entered, release = threading.Event(), threading.Event()
    original = service.inbound.client.read_issue

    def blocked(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        if threading.current_thread().name == "linear-inbound":
            entered.set()
            assert release.wait(5), "Authored readback gate deadline"
        return snapshot

    monkeypatch.setattr(service.inbound.client, "read_issue", blocked)
    receiver.start()
    try:
        # This control targets webhook consumption; periodic readback revocation
        # has its own SR03 control. Finish the unchanged startup safety scan first.
        wait_for(lambda: receiver.next_reconciliation > 0)
        if branch != "echo":
            fixture.provider.edit(
                fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"}
            )
        headers, body = fixture.event()
        if branch == "reordered":
            fixture.provider.edit(fixture.task.issue_id, title=fixture.task.title)
        before = service.core.get_task(service.handle, fixture.task.id)
        assert request(receiver.http.address, ROUTE, headers, body)[0] == 200
        assert entered.wait(3)
        if fault == "revoke":
            revoke(receiver)
        elif fault == "expire":
            monkeypatch.setattr(receiver.observations.journal, "clock", lambda: time.time() + 3600)
        elif fault == "config":
            write_private(fixture.receiver, {**fixture.values, "port": 1}, replace=True)
        elif fault == "secret":
            write_private(
                fixture.values["secret_file"], {"secret": "changed-fixture-secret"}, replace=True
            )
        else:
            (fixture.root / "credentials.env").write_text(
                "LINEAR_TEST_USER_A_TOKEN=changed-fixture-token\n"
            )
        with pytest.raises(AccessDenied):
            receiver.check()
        assert service.core.session_binding(service.handle)
        release.set()
        wait_for(lambda: receiver.failed)
        assert receiver.failed == "receiver_drain_failed"
        assert service.core.get_task(service.handle, fixture.task.id) == before
        assert not service.core.external.observations(service.handle, fixture.task.id)
        assert_recoverable(service)
    finally:
        release.set()
        cleanup = receiver.stop()
        (fixture.root / "cleanup.json").write_text(json.dumps(cleanup))
        assert cleanup["stopped"] and cleanup["ownership_released"]
        assert not receiver.worker.is_alive()


@pytest.mark.parametrize(
    "boundary", ["evidence", "settled", "echo_receipt", "remove", "scope_remove"]
)
def test_ra03_ra04_later_consumption_checks_preserve_committed_prefix(
    fixture, monkeypatch, boundary
):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    snapshots = []

    def lose_authority():
        snapshots.append(service.core.get_task(service.handle, fixture.task.id))
        revoke(receiver)

    if boundary == "evidence":

        def evidence(*_):
            lose_authority()
            return {}

        monkeypatch.setattr(service.inbound, "evidence_for", evidence)
    else:
        owner, name = (
            (service.core.external, "settle")
            if boundary == "settled"
            else (
                service.inbound.journal,
                {
                    "echo_receipt": "application_receipt",
                    "remove": "removal_receipt",
                    "scope_remove": "scope_removal_receipt",
                }[boundary],
            )
        )
        original = getattr(owner, name)

        def completed(*args, **kwargs):
            result = original(*args, **kwargs)
            lose_authority()
            return result

        monkeypatch.setattr(owner, name, completed)
    if boundary in {"evidence", "settled"}:
        fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    if boundary == "scope_remove":
        fixture.provider.edit(fixture.task.issue_id, project={"id": uid(543)})
    headers, body = fixture.event(action="remove") if boundary == "remove" else fixture.event()
    assert receiver.receive("POST", headers, body).status == 200
    try:
        with pytest.raises(AccessDenied):
            service.inbound.run_once(service.handle, check_authority=receiver.check)
        assert len(snapshots) == 1
        assert service.core.get_task(service.handle, fixture.task.id) == snapshots[0]
        assert_recoverable(service)
    finally:
        assert receiver.stop()["stopped"]


def test_ra04_recovered_application_rechecks_authority_before_receipt(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    headers, body = fixture.event()
    assert receiver.receive("POST", headers, body).status == 200
    with monkeypatch.context() as patch:

        def crash(*_):
            raise RuntimeError("Authored crash after legitimate Core application")

        patch.setattr(service.core.sync, "mark_applied", crash)
        with pytest.raises(RuntimeError):
            service.inbound.run_once(service.handle, check_authority=receiver.check)
    before = service.core.get_task(service.handle, fixture.task.id)
    assert before.observed_status == "Todo" and before.decision_revision == 1
    monkeypatch.setattr(service.inbound, "clock", lambda: time.time() + 31)
    original = service.inbound.journal.application_receipt

    def revoked_receipt(*args, **kwargs):
        result = original(*args, **kwargs)
        revoke(receiver)
        return result

    monkeypatch.setattr(service.inbound.journal, "application_receipt", revoked_receipt)
    try:
        with pytest.raises(AccessDenied):
            service.inbound.run_once(service.handle, check_authority=receiver.check)
        assert service.core.get_task(service.handle, fixture.task.id) == before
        assert_recoverable(service)
    finally:
        assert receiver.stop()["stopped"]


def test_ra02_failed_readback_after_revocation_is_recoverable_not_conflicted(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    before = service.core.get_task(service.handle, fixture.task.id)
    original = service.inbound.client.read_issue

    def lost_readback(*args, **kwargs):
        original(*args, **kwargs)
        revoke(receiver)
        raise LinearError("transport_failure", uncertain=True)

    monkeypatch.setattr(service.inbound.client, "read_issue", lost_readback)
    headers, body = fixture.event()
    assert receiver.receive("POST", headers, body).status == 200
    try:
        with pytest.raises(AccessDenied):
            service.inbound.run_once(service.handle, check_authority=receiver.check)
        assert service.core.get_task(service.handle, fixture.task.id) == before
        assert_recoverable(service)
    finally:
        assert receiver.stop()["stopped"]
