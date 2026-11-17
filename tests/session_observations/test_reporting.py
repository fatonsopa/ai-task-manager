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
