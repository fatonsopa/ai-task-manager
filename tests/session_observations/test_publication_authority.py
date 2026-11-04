"""Installed lead authority controls with authored transport and owned PTY fixtures."""

import os
import select
from dataclasses import replace

import pytest

from ai_task_manager.adapters.hosts.bridge.startup import StartupObserver
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native import connect_lead
from tests.hosts.conftest import FixturePolicy
from tests.hosts.test_notifications import acknowledged, delivered, reported
from tests.session_observations.test_host_notifications import (
    binding as binding,
)
from tests.session_observations.test_host_notifications import (
    compose,
    lead_enrollment,
    receipt_request,
)
from tests.session_observations.test_host_notifications import (
    journal as journal,
)
from tests.session_observations.test_host_notifications import (
    native as native,
)
from tests.session_observations.test_host_notifications import (
    provider as provider,
)
from tests.session_observations.test_host_notifications import (
    service as service,
)
from tests.session_observations.test_observations import _confirmed_task_for_retry


def pending_report(provider, service, journal, native, binding):
    event, process, emit, observer, value, gateway, sink = compose(
        provider, service, journal, native, binding
    )
    delivered(emit, observer, value)
    acknowledged(emit, value)
    gateway.call("acknowledge_event", {"event_id": event.id, "challenge": value.challenge})
    reported(
        emit,
        item={
            "type": "agentMessage",
            "id": "output",
            "phase": "final_answer",
            "text": f"Event {event.id} completed; evidence retained.",
        },
    )
    return event, emit, observer, gateway


@pytest.mark.parametrize(
    "fault", ["revoked", "expired", "provider", "native_account", "generation", "session", "config"]
)
def test_PA01_pending_publication_refuses_before_terminal_bytes(
    provider, service, journal, native, binding, monkeypatch, fault
):
    event, emit, observer, gateway = pending_report(provider, service, journal, native, binding)
    active = gateway.slot._service
    if fault == "revoked":
        active.decisions.observations.revoke()
    elif fault == "expired":
        active.decisions.journal.clock = lambda: provider.deadline + 1
    elif fault == "provider":

        def unavailable():
            raise ConnectionError("Declared provider unavailable")

        monkeypatch.setattr(active.decisions.observations.client, "verify_binding", unavailable)
    elif fault == "native_account":
        with pytest.raises(AccessDenied):
            emit({"method": "account/updated", "params": {"authMode": "apikey"}})
    elif fault == "session":
        active.core.revoke_session(active.handle)
    elif fault == "config":
        from ai_task_manager.config.files import read_private, write_private

        config = read_private(provider.config)
        config["approval_policy"]["style_hash"] = "d" * 64
        write_private(provider.config, config, replace=True)
    else:
        gateway.slot.generation += 1
    with pytest.raises((AccessDenied, ConnectionError)):
        gateway.publish(event.id)
    master = provider.lead_connections[-1][1]
    assert not select.select([master], [], [], 0)[0], "Unauthorized report reached terminal"
    if fault != "session":
        assert "user_reported" not in service.core.notifications.receipts(service.handle, event.id)
    assert (event.id, "user_reported") not in observer.receipts


def claude_receipt(provider, service, journal, native, binding):
    _confirmed_task_for_retry(service)
    event = service.core.notifications.list_events(service.handle)[0]
    process, transcript, emit = native()
    transcript.binding = replace(binding, host="claude")
    policy = FixturePolicy(binding.configuration_digest, process.command.cwd, {})
    policy.model = "fixture-model"
    auth = journal._append(
        "trace",
        {
            "auth_method": "claude.ai",
            "account_hash": "fixture-account",
            "configuration_digest": binding.configuration_digest,
        },
    )
    startup = StartupObserver(
        transcript, policy, expected_account="fixture-account", claude_auth_ref=auth
    )
    init = {
        "type": "system",
        "subtype": "init",
        "session_id": "claude-session",
        "model": "fixture-model",
        "apiKeySource": "none",
        "cwd": str(policy.cwd),
        "claude_code_version": "2.1.301",
        "tools": [],
        "mcp_servers": [],
    }
    emit(init)
    lead_ref = lead_enrollment(journal, transcript.binding, startup)
    master, slave = os.openpty()
    try:
        connection = connect_lead(
            provider.config,
            transcript,
            startup,
            lead_ref,
            slave,
            linear_components=provider.components(),
        )
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    provider.lead_connections.append((connection, master, slave))
    observer = connection.notifications
    value = observer.prepare(event.id, "Inspect the actual scoped event.")
    emit(
        {
            "type": "user",
            "isReplay": True,
            "uuid": value.input_id,
            "session_id": "claude-session",
            "message": {"content": observer.context(event.id)},
        }
    )
    return event, emit, observer, init


@pytest.mark.parametrize("state", ["fresh", "interrupted", "committed", "valid"])
def test_PA02_cold_claude_authentication_fence_preserves_completed_facts(
    provider, service, journal, native, binding, monkeypatch, state
):
    event, emit, observer, init = claude_receipt(provider, service, journal, native, binding)
    request = receipt_request(event, observer)
    if state == "committed":
        assert service.call("event_acknowledge", request)["ok"]
    elif state == "interrupted":

        def interrupted(*args, **kwargs):
            raise ConnectionError("Declared saved-proof interruption")

        monkeypatch.setattr(service.core.notifications, "acknowledge", interrupted)
        assert not service.call("event_acknowledge", request)["ok"]
    before = service.core.notifications.receipts(service.handle, event.id)
    if state != "valid":
        with pytest.raises(AccessDenied):
            emit({**init, "apiKeySource": "environment"})
    cold = load_service(provider.config, linear_components=provider.components())
    result = cold.call("event_acknowledge", request)
    assert result["ok"] is (state in {"committed", "valid"})
    if state != "valid":
        assert cold.core.notifications.receipts(cold.handle, event.id) == before


@pytest.mark.parametrize("committed", [False, True])
def test_PA03_published_output_retry_never_reprints_and_preserves_commit(
    provider, service, journal, native, binding, monkeypatch, committed
):
    event, _, observer, gateway = pending_report(provider, service, journal, native, binding)
    active = gateway.slot._service
    original = active.core.notifications.acknowledge

    def interrupted(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise ConnectionError("Declared report acknowledgement reply loss")

    monkeypatch.setattr(active.core.notifications, "acknowledge", interrupted)
    with pytest.raises(AccessDenied):
        gateway.publish(event.id)
    master = provider.lead_connections[-1][1]
    assert event.id.encode() in os.read(master, 65536)
    monkeypatch.setattr(active.core.notifications, "acknowledge", original)
    active.decisions.observations.revoke()
    if committed:
        assert gateway.publish(event.id)
    else:
        with pytest.raises(AccessDenied):
            gateway.publish(event.id)
    assert not select.select([master], [], [], 0)[0]
    assert (
        "user_reported" in service.core.notifications.receipts(service.handle, event.id)
    ) is committed


def test_PA04_publication_provider_check_runs_outside_core_transaction(
    provider, service, journal, native, binding, monkeypatch
):
    from contextlib import contextmanager

    event, _, _, gateway = pending_report(provider, service, journal, native, binding)
    active = gateway.slot._service
    inside, checks = [], []
    original = active.core.context.db.transaction
    verify = active.decisions.observations.client.verify_binding

    @contextmanager
    def transaction(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            inside.append(True)
            try:
                yield conn
            finally:
                inside.pop()

    def external_verify():
        assert not inside, "Provider verification occurred inside Core"
        checks.append(True)
        return verify()

    monkeypatch.setattr(active.core.context.db, "transaction", transaction)
    monkeypatch.setattr(active.decisions.observations.client, "verify_binding", external_verify)
    assert gateway.publish(event.id)
    assert checks
    master = provider.lead_connections[-1][1]
    assert event.id.encode() in os.read(master, 65536)
