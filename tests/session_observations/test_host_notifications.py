"""Declared HTTP and native protocol fixtures; no real host or provider acceptance."""

import json
import os
import select
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.lead import LeadToolGateway
from ai_task_manager.adapters.hosts.bridge.observations import NotificationObserver
from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.config.files import digest
from ai_task_manager.config.managed import ManagedServiceSlot
from ai_task_manager.config.service import load_service
from ai_task_manager.installation.native import connect_lead
from ai_task_manager.installation.sessions import setup
from tests.hosts.conftest import native as native_fixture
from tests.hosts.test_notifications import FixtureSink, acknowledged, delivered, reported
from tests.hosts.test_startup import codex_start
from tests.session_observations.helpers import OwnedProvider
from tests.session_observations.test_observations import _confirmed_task_for_retry


@pytest.fixture
def native(tmp_path, journal, binding):
    generator = native_fixture.__wrapped__(tmp_path, journal, binding)
    create = next(generator)
    processes = []

    def owned(*args, **kwargs):
        result = create(*args, **kwargs)
        processes.append(result[0])
        return result

    try:
        yield owned
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
        for process in processes:
            assert process._closed and process.process.poll() is not None
            evidence = os.environ.get("HOST_OBSERVATION_CLEANUP")
            if evidence:
                with Path(evidence).open("a") as output:
                    output.write(
                        json.dumps(
                            {"pid": process.identity.pid, "stop": asdict(process.stop_evidence)}
                        )
                        + "\n"
                    )


@pytest.fixture
def provider(tmp_path):
    value = OwnedProvider(tmp_path / "provider")
    value.lead_connections = []
    try:
        setup(
            value.config,
            logical_session="lead-session",
            actions=("enqueue",),
            deadline=value.deadline,
            operation_key="host-session",
            components=value.components(),
        )
        yield value
    finally:
        for connection, master, slave in value.lead_connections:
            connection.close()
            os.close(master)
            os.close(slave)
        value.close()


@pytest.fixture
def service(provider):
    return load_service(provider.config, linear_components=provider.components())


@pytest.fixture
def journal(tmp_path):
    root = tmp_path / "native"
    root.mkdir(mode=0o700)
    return HostJournal(root / "host.sqlite", root / "host.key")


@pytest.fixture
def binding(service):
    return HostBinding(
        service.scope,
        service.session_id,
        "native-attempt",
        "native-worker",
        "codex",
        "principal-ref",
        "a" * 64,
        time.time() + 60,
    )


def lead_enrollment(journal, binding, startup):
    class DeclaredOperatorReply(OperatorChannel):
        # Authored operator-input contract only, never actual human acceptance.
        def _exchange(self, request, timeout):
            return {
                "request_id": request["request_id"],
                "challenge": request["challenge"],
                "accepted": True,
                "instruction_hash": digest(request["instruction"]),
            }, os.getpid()

    return DeclaredOperatorReply(journal).capture_enrollment(
        scope=binding.scope, actions=("enqueue",), startup_ref=startup.ready_ref
    )


def compose(provider, service, journal, native, binding):
    _confirmed_task_for_retry(service)
    event = service.core.notifications.list_events(service.handle)[0]
    process, transcript, emit, startup = codex_start(native, binding)
    lead_ref = lead_enrollment(journal, binding, startup)
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
    observer, gateway = connection.notifications, connection.gateway
    value = observer.prepare(event.id, "Inspect the actual scoped event.")
    return event, process, emit, observer, value, gateway, connection.output


def test_HO01_provider_session_consumes_native_gateway_and_cold_receipts(
    provider, service, journal, native, binding
):
    event, process, emit, observer, value, gateway, sink = compose(
        provider, service, journal, native, binding
    )
    delivered(emit, observer, value)
    acknowledged(emit, value)
    assert gateway.call("acknowledge_event", {"event_id": event.id, "challenge": value.challenge})
    reported(
        emit,
        item={
            "type": "agentMessage",
            "id": "output",
            "phase": "final_answer",
            "text": f"Event {event.id} completed; evidence is retained.",
        },
    )
    assert gateway.publish(event.id)
    cold = load_service(provider.config, linear_components=provider.components())
    assert set(cold.core.notifications.receipts(cold.handle, event.id)) == {
        "queued",
        "delivered",
        "ai_acknowledged",
        "user_reported",
    }
    observed = cold.decisions.observations.verify_host_observation(
        observer.reference(event.id, "user_reported")
    )
    assert observed.scope == cold.scope and observed.session_id == cold.session_id
    master = provider.lead_connections[-1][1]
    assert event.id.encode() in os.read(master, 65536)
    assert gateway.publish(event.id)
    assert not select.select([master], [], [], 0)[0]
    assert (
        cold.decisions.observations.verify_enrollment(cold.grant_ref).source_ref == cold.grant_ref
    )
    process.stop()


def receipt_request(event, observer):
    return {
        "event_id": event.id,
        "stage": "delivered",
        "observation_ref": observer.reference(event.id, "delivered"),
        "operation_key": "original-delivery",
    }


@pytest.mark.parametrize("fault", ["revoked", "expired", "provider", "config", "key", "store"])
def test_HO02_current_authority_and_original_source_required(
    provider, service, journal, native, binding, fault
):
    from dataclasses import replace

    from ai_task_manager.config.files import read_private, write_private

    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    request = receipt_request(event, observer)
    if fault == "revoked":
        service.decisions.observations.revoke()
    elif fault == "expired":
        service.decisions.journal.clock = lambda: provider.deadline + 1
    elif fault == "provider":
        provider.actual = replace(provider.actual, viewer_id="00000000-0000-0000-0000-000000000001")
    elif fault == "config":
        config = read_private(provider.config)
        config["approval_policy"]["style_hash"] = "d" * 64
        write_private(provider.config, config, replace=True)
    elif fault == "key":
        journal.key_path.write_bytes(b"x" * 32)
    else:
        original = journal.path.read_bytes()
        journal.path.rename(journal.path.with_suffix(".original"))
        journal.path.write_bytes(original)
        journal.path.chmod(0o600)
    assert not service.call("event_acknowledge", request)["ok"]
    assert set(service.core.notifications.receipts(service.handle, event.id)) == {"queued"}


@pytest.mark.parametrize("fault", ["foreign_event", "stage", "self_report", "human"])
def test_HO03_receipt_cannot_select_event_stage_or_human_authority(
    provider, service, journal, native, binding, fault
):
    from ai_task_manager.domains.work.errors import EvidenceRequired

    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    request = receipt_request(event, observer)
    if fault == "foreign_event":
        request["event_id"] = "unowned-event"
    elif fault == "stage":
        request["stage"] = "ai_acknowledged"
    elif fault == "self_report":
        request["observation_ref"] = '{"delivered":true}'
    else:
        with pytest.raises(EvidenceRequired):
            service.decisions.observations.verify_instruction(request["observation_ref"])
        return
    assert not service.call("event_acknowledge", request)["ok"]
    assert set(service.core.notifications.receipts(service.handle, event.id)) == {"queued"}


@pytest.mark.parametrize("committed", [False, True])
def test_HO04_interrupted_receipt_revalidates_uncommitted_but_recovers_committed(
    provider, service, journal, native, binding, monkeypatch, committed
):
    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    request = receipt_request(event, observer)
    original = service.core.notifications.acknowledge

    def interrupted(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise ConnectionError("Declared reply loss at receipt boundary")

    monkeypatch.setattr(service.core.notifications, "acknowledge", interrupted)
    assert not service.call("event_acknowledge", request)["ok"]
    before = service.core.notifications.receipts(service.handle, event.id)
    service.decisions.observations.revoke()
    monkeypatch.setattr(service.core.notifications, "acknowledge", original)
    assert service.call("event_acknowledge", request)["ok"] is committed
    assert service.core.notifications.receipts(service.handle, event.id) == before


def test_HO05_revocation_after_prevalidation_refuses_inside_core(
    provider, service, journal, native, binding, monkeypatch
):
    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    request = receipt_request(event, observer)
    validate = service.decisions.validate_notification_source

    def revoke_after(*args):
        validate(*args)
        service.decisions.observations.revoke()

    monkeypatch.setattr(service.decisions, "validate_notification_source", revoke_after)
    assert not service.call("event_acknowledge", request)["ok"]
    assert set(service.core.notifications.receipts(service.handle, event.id)) == {"queued"}


def test_HO06_provider_io_stays_outside_core_transaction(
    provider, service, journal, native, binding, monkeypatch
):
    from contextlib import contextmanager

    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    inside = []
    original = service.core.context.db.transaction
    verify = service.decisions.observations.client.verify_binding

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
        return verify()

    monkeypatch.setattr(service.core.context.db, "transaction", transaction)
    monkeypatch.setattr(service.decisions.observations.client, "verify_binding", external_verify)
    assert service.call("event_acknowledge", receipt_request(event, observer))["ok"]


@pytest.mark.parametrize("fault", ["scope", "session", "producer", "stopped"])
def test_HO07_gateway_refuses_foreign_or_unavailable_native_producer(
    provider, service, journal, native, binding, fault
):
    from dataclasses import replace

    from ai_task_manager.domains.work.errors import AccessDenied

    process, transcript, _, startup = codex_start(native, binding)
    observer = NotificationObserver(transcript, startup, FixtureSink(journal))
    lead_ref = lead_enrollment(journal, binding, startup)
    if fault == "scope":
        observer.binding = transcript.binding = replace(
            binding, scope=replace(binding.scope, project="foreign")
        )
    elif fault == "session":
        observer.binding = transcript.binding = replace(binding, core_session="foreign")
    elif fault == "producer":
        observer = type("SelfReport", (), {"journal": journal})()
    else:
        process.stop()
    slot = ManagedServiceSlot.configured(
        provider.config, journal, linear_components=provider.components()
    )
    with pytest.raises(AccessDenied):
        LeadToolGateway(
            slot, observer, allowed_operations=("event_list",), lead_enrollment_ref=lead_ref
        )


def test_HO08_source_tampering_and_missing_receipt_do_not_create_observation(
    provider, service, journal, native, binding
):
    from ai_task_manager.domains.work.errors import AccessDenied

    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    request = receipt_request(event, observer)
    row = journal._read(request["observation_ref"])
    source = row["sources"][0]
    with journal.transaction() as conn:
        conn.execute("UPDATE host_records SET data='{}' WHERE ref=?", (source,))
    with pytest.raises(AccessDenied):
        service.decisions.observations.verify_host_observation(request["observation_ref"])
    assert set(service.core.notifications.receipts(service.handle, event.id)) == {"queued"}


@pytest.mark.parametrize("fault", ["provider", "config", "key"])
def test_HO09_cached_proof_refreshes_external_source_before_retry(
    provider, service, journal, native, binding, monkeypatch, fault
):
    from dataclasses import replace

    from ai_task_manager.config.files import read_private, write_private

    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    request = receipt_request(event, observer)
    original = service.core.notifications.acknowledge

    def interrupted(*args, **kwargs):
        raise ConnectionError("Receipt proof saved; Core not invoked")

    monkeypatch.setattr(service.core.notifications, "acknowledge", interrupted)
    assert not service.call("event_acknowledge", request)["ok"]
    monkeypatch.setattr(service.core.notifications, "acknowledge", original)
    if fault == "provider":
        provider.actual = replace(provider.actual, viewer_id="00000000-0000-0000-0000-000000000001")
    elif fault == "config":
        config = read_private(provider.config)
        config["approval_policy"]["style_hash"] = "d" * 64
        write_private(provider.config, config, replace=True)
        service = load_service(provider.config, linear_components=provider.components())
    else:
        journal.key_path.write_bytes(b"x" * 32)
    assert not service.call("event_acknowledge", request)["ok"]
    assert set(service.core.notifications.receipts(service.handle, event.id)) == {"queued"}


@pytest.mark.parametrize("fault", ["expired_source", "parent_permissions", "native_account"])
def test_HO10_native_source_continuity_after_binding(
    provider, service, journal, native, binding, fault
):
    from ai_task_manager.domains.work.errors import AccessDenied

    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    if fault == "expired_source":
        service.decisions.journal.clock = lambda: binding.deadline + 1
    elif fault == "parent_permissions":
        journal.path.parent.chmod(0o755)
    else:
        with pytest.raises(AccessDenied):
            emit({"method": "account/updated", "params": {"authMode": "apikey"}})
    try:
        assert not service.call("event_acknowledge", receipt_request(event, observer))["ok"]
        assert set(service.core.notifications.receipts(service.handle, event.id)) == {"queued"}
    finally:
        journal.path.parent.chmod(0o700)


def test_HO11_cold_installed_cli_consumes_bound_receipt_and_replays(
    provider, service, journal, native, binding
):
    event, _, emit, observer, value, _, _ = compose(provider, service, journal, native, binding)
    delivered(emit, observer, value)
    request = receipt_request(event, observer)
    # This bootstrap only selects the authored loopback endpoint. All configuration,
    # enrollment, native-source and receipt checks execute in the installed package.
    script = """import sys
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.presentation.cli.main import main
HttpTransport.ENDPOINT=sys.argv[1]
raise SystemExit(main(['--config',sys.argv[2],'--json','event','acknowledge','--input','-']))
"""
    arguments = [
        os.environ["SESSION_OBSERVATIONS_PYTHON"],
        "-I",
        "-c",
        script,
        provider.endpoint,
        str(provider.config),
    ]
    results = []
    for _ in range(2):
        with subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                stdout, stderr = child.communicate(json.dumps(request), timeout=15)
            except BaseException:
                child.kill()
                child.wait(timeout=5)
                raise
            results.append((child.returncode, stdout, stderr))
            evidence = os.environ.get("HOST_OBSERVATION_CLEANUP")
            if evidence:
                with Path(evidence).open("a") as output:
                    output.write(
                        json.dumps(
                            {
                                "pid": child.pid,
                                "kind": "cold_cli",
                                "returncode": child.returncode,
                                "reaped": True,
                            }
                        )
                        + "\n"
                    )
    assert all(code == 0 and not error for code, _, error in results), results
    assert all(json.loads(output)["ok"] for _, output, _ in results)
    assert set(service.core.notifications.receipts(service.handle, event.id)) == {
        "queued",
        "delivered",
    }


@pytest.mark.parametrize("fault", ["missing_enrollment", "foreign_enrollment", "worker_attempt"])
def test_HO12_installed_connect_refuses_worker_or_unbound_lead(
    provider, service, journal, native, binding, fault
):
    from ai_task_manager.domains.work.errors import AccessDenied

    process, transcript, _, startup = codex_start(native, binding)
    lead_ref = lead_enrollment(journal, binding, startup)
    if fault == "missing_enrollment":
        lead_ref = startup.ready_ref  # A genuine startup alone is not lead authority.
    elif fault == "foreign_enrollment":
        _, _, _, other_startup = codex_start(native, binding)
        lead_ref = lead_enrollment(journal, binding, other_startup)
    else:
        # Authored negative sentinel, not an actual worker-start acceptance claim.
        # Even an unresolved Core worker identity must prevent role promotion.
        task = _confirmed_task_for_retry(service)
        with service.core.context.db.transaction() as conn:
            conn.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?)",
                (binding.attempt_id, task.id, service.session_id, binding.host, 1, "{}"),
            )
    before = list(transcript._listeners)
    master, slave = os.openpty()
    try:
        with pytest.raises(AccessDenied):
            connect_lead(
                provider.config,
                transcript,
                startup,
                lead_ref,
                slave,
                linear_components=provider.components(),
            )
        assert transcript._listeners == before
        assert not select.select([master], [], [], 0)[0]
    finally:
        os.close(master)
        os.close(slave)
    assert process.process.poll() is None  # Failed connect does not steal native ownership.
