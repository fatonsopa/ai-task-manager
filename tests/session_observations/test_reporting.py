"""Authored HTTP identity and real Core controls for event-only reporting."""

import time

import pytest

from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.sessions import setup
from ai_task_manager.installation.sessions.reporting import setup as reporting_setup
from tests.session_observations.helpers import OwnedProvider
from tests.session_observations.test_observations import _confirmed_task_for_retry


@pytest.fixture
def retained(tmp_path):
    p = OwnedProvider(tmp_path / "owned")
    try:
        setup(
            p.config,
            logical_session="lead-session",
            actions=("enqueue",),
            deadline=p.deadline,
            operation_key="initial",
            components=p.components(),
        )
        s = load_service(p.config, linear_components=p.components())
        _confirmed_task_for_retry(s)
        event = s.core.notifications.list_events(s.handle)[-1]
        s.decisions.observations.revoke()
        original = read_private(p.config)
        destination = p.root / "report.json"
        write_private(
            destination,
            {
                k: v
                for k, v in original.items()
                if k not in {"grant_ref", "session_observations", "workflow_control"}
            },
        )
        yield p, s, event, destination
    finally:
        p.close()


def test_reporting_reuses_valid_capability_after_api_revocation(retained):
    p, old, event, destination = retained
    before = (p.root / "session.json").read_bytes()
    result = reporting_setup(
        destination,
        p.config,
        event_id=event.id,
        deadline=time.time() + 60,
        operation_key="report",
        components=p.components(),
    )
    s = load_service(destination, linear_components=p.components())
    assert s.session_id == old.session_id
    assert (p.root / "session.json").read_bytes() == before
    assert s.decisions.observations.verify_enrollment(s.grant_ref).actions == ()
    assert result["event_id"] == event.id
    assert [x["id"] for x in s.call("event_list", {})["data"]] == [event.id]
    assert not s.call("task_list", {})["ok"]
    with pytest.raises(AccessDenied):
        s._invoke("task_list", {}, None)
    assert not s.call(
        "event_acknowledge",
        {
            "event_id": "foreign",
            "stage": "delivered",
            "observation_ref": "foreign",
            "operation_key": "foreign",
        },
    )["ok"]
    with pytest.raises(AccessDenied):
        old.decisions.observations.verify_enrollment(old.grant_ref)


def test_reporting_refuses_missing_event_and_generic_adoption(retained):
    p, _, _, destination = retained
    with pytest.raises(Exception):
        reporting_setup(
            destination,
            p.config,
            event_id="foreign",
            deadline=time.time() + 60,
            operation_key="report",
            components=p.components(),
        )
    assert "grant_ref" not in read_private(destination)
    with pytest.raises(AccessDenied, match="adopt"):
        setup(
            destination,
            logical_session="lead-session",
            actions=("enqueue",),
            deadline=time.time() + 60,
            operation_key="generic",
            components=p.components(),
        )


def test_reporting_fences_current_task_and_replaced_capability(retained):
    p, _, event, destination = retained
    reporting_setup(
        destination,
        p.config,
        event_id=event.id,
        deadline=time.time() + 60,
        operation_key="report",
        components=p.components(),
    )
    s = load_service(destination, linear_components=p.components())
    with s.core.context.db.transaction() as conn:
        from ai_task_manager.storage import work

        task = s.core.get_task(s.handle, event.task_id, connection=conn)
        work.save(conn, task, task.version)
    assert not s.call("event_list", {})["ok"]
    with s.core.context.db.transaction() as conn:
        from ai_task_manager.storage.database import record

        conn.execute(
            "UPDATE tasks SET version=?,data=? WHERE id=?", (task.version, record(task), task.id)
        )
    path = p.root / "session.json"
    original = path.read_bytes()
    path.rename(p.root / "retained-session.json")
    path.write_bytes(original)
    path.chmod(0o600)
    assert not s.call("event_list", {})["ok"]


@pytest.mark.parametrize(
    "changed_before_output", [False, True], ids=["retained-positive", "changed-task-zero-output"]
)
def test_reporting_connected_native_receipt_consumer(tmp_path, changed_before_output):
    import os

    from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
    from ai_task_manager.adapters.hosts.bridge.records import HostBinding
    from ai_task_manager.config.files import digest
    from ai_task_manager.installation.native import connect_lead
    from tests.hosts.conftest import native as native_fixture
    from tests.hosts.test_notifications import acknowledged, delivered, reported
    from tests.hosts.test_startup import codex_start

    p = OwnedProvider(tmp_path / "owned")
    connection = None
    generator = None
    master = slave = None
    try:
        setup(
            p.config,
            logical_session="lead-session",
            actions=("enqueue",),
            deadline=p.deadline,
            operation_key="initial",
            components=p.components(),
        )
        old = load_service(p.config, linear_components=p.components())
        _confirmed_task_for_retry(old)
        event = old.core.notifications.list_events(old.handle)[-1]
        old.decisions.observations.revoke()
        config = p.root / "report.json"
        write_private(
            config,
            {
                k: v
                for k, v in read_private(p.config).items()
                if k not in {"grant_ref", "session_observations", "workflow_control"}
            },
        )
        reporting_setup(
            config,
            p.config,
            event_id=event.id,
            deadline=time.time() + 60,
            operation_key="report",
            components=p.components(),
        )
        native_root = tmp_path / "native"
        native_root.mkdir(mode=0o700)
        journal = HostJournal(native_root / "host.sqlite", native_root / "host.key")
        binding = HostBinding(
            old.scope,
            old.session_id,
            "native-attempt",
            "native-worker",
            "codex",
            "principal-ref",
            "a" * 64,
            time.time() + 60,
        )
        generator = native_fixture.__wrapped__(tmp_path, journal, binding)
        native = next(generator)
        process, transcript, emit, startup = codex_start(native, binding)

        class AuthoredOperator(OperatorChannel):
            def _exchange(self, request, timeout):
                return {
                    "request_id": request["request_id"],
                    "challenge": request["challenge"],
                    "accepted": True,
                    "instruction_hash": digest(request["instruction"]),
                }, os.getpid()

        ref = AuthoredOperator(journal).capture_enrollment(
            scope=binding.scope, actions=(), startup_ref=startup.ready_ref
        )
        master, slave = os.openpty()
        connection = connect_lead(
            config, transcript, startup, ref, slave, linear_components=p.components()
        )
        observer, gateway = connection.notifications, connection.gateway
        value = observer.prepare(event.id, "Inspect the selected current event.")
        delivered(emit, observer, value)
        acknowledged(emit, value)
        gateway.call("acknowledge_event", {"event_id": event.id, "challenge": value.challenge})
        reported(
            emit,
            item={
                "type": "agentMessage",
                "id": "output",
                "phase": "final_answer",
                "text": f"Event {event.id} completed; evidence is retained.",
            },
        )
        if changed_before_output:
            import select

            from ai_task_manager.storage import work

            with old.core.context.db.transaction() as conn:
                task = old.core.get_task(old.handle, event.task_id, connection=conn)
                work.save(conn, task, task.version)
            with pytest.raises(AccessDenied, match="task version"):
                gateway.publish(event.id)
            assert not select.select([master], [], [], 0)[0]
            assert "user_reported" not in old.core.notifications.receipts(old.handle, event.id)
            process.stop()
            return
        gateway.publish(event.id)
        cold = load_service(config, linear_components=p.components())
        assert set(cold.core.notifications.receipts(cold.handle, event.id)) == {
            "queued",
            "delivered",
            "ai_acknowledged",
            "user_reported",
        }
        assert set(gateway.schemas) == {"session_show", "event_list", "acknowledge_event"}
        assert gateway.publish(event.id)
        process.stop()
    finally:
        if connection:
            connection.close()
        if master is not None:
            os.close(master)
            os.close(slave)
        if generator:
            try:
                next(generator)
            except StopIteration:
                pass
        p.close()


def test_reporting_direct_configured_business_consumers_refuse(retained):
    from ai_task_manager.application.linear_sync.inbound import InboundDrain
    from ai_task_manager.application.linear_sync.outbound import OutboundDrain

    p, old, event, destination = retained
    reporting_setup(
        destination,
        p.config,
        event_id=event.id,
        deadline=time.time() + 60,
        operation_key="report",
        components=p.components(),
    )
    s = load_service(destination, linear_components=p.components())
    assert isinstance(s.provider, OutboundDrain)
    assert isinstance(s.inbound, InboundDrain)
    before = old.core.sync.list_operations(old.handle)
    requests = p.requests
    for effect in (
        lambda: s.provider.run_once(s.handle),
        lambda: s.inbound.run_once(s.handle),
        lambda: s.publisher(),
    ):
        with pytest.raises(AccessDenied, match="provider business effects"):
            effect()
    assert p.requests == requests
    assert old.core.sync.list_operations(old.handle) == before
