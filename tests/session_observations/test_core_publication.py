"""Declared native/PTY controls for Core-backed public notification eligibility."""

import select
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.domains.work.errors import AccessDenied, WorkError
from ai_task_manager.storage import events, sessions
from tests.hosts.test_notifications import acknowledged, delivered, reported
from tests.session_observations.test_host_notifications import (
    binding as binding,
)
from tests.session_observations.test_host_notifications import (
    compose,
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


@pytest.mark.parametrize("state", ["missing", "foreign", "queued", "delivered", "failed_ack"])
def test_CP01_core_event_and_accepted_stages_precede_public_output(
    provider, service, journal, native, binding, monkeypatch, state
):
    event, _, emit, observer, value, gateway, _ = compose(
        provider, service, journal, native, binding
    )
    target = event.id
    if state == "missing":
        target = str(uuid4())
    elif state == "foreign":
        # Authored second-session event sentinel, never provider or human authority.
        with service.core.context.db.transaction() as conn:
            foreign = sessions.register(conn, replace(service.scope, logical_session="other"))
            sid, _ = sessions.resolve(conn, foreign)
            target = events.append(conn, sid, event.task_id, event.kind, {}).id
    if target != event.id:
        value = observer.prepare(target, "Inspect the selected event.")
    delivered(emit, observer, value)
    acknowledged(emit, value)
    active = gateway.slot._service
    if state == "delivered":
        assert active.call("event_acknowledge", receipt_request(event, observer))["ok"]
    elif state in {"missing", "foreign", "failed_ack"}:
        if state == "failed_ack":

            def unavailable(*args, **kwargs):
                raise ConnectionError("Declared Core acknowledgement interruption")

            monkeypatch.setattr(active.core.notifications, "acknowledge", unavailable)
        with pytest.raises(AccessDenied):
            gateway.call("acknowledge_event", {"event_id": target, "challenge": value.challenge})
    reported(
        emit,
        item={
            "type": "agentMessage",
            "id": "final-output",
            "phase": "final_answer",
            "text": f"Event {target} has a final native response.",
        },
    )
    assert observer.reference(target, "ai_acknowledged")
    assert target in observer.outputs
    before = service.core.notifications.receipts(service.handle, event.id)
    with pytest.raises(WorkError):
        gateway.publish(target)
    master = provider.lead_connections[-1][1]
    assert not select.select([master], [], [], 0)[0], "Rejected Core event reached terminal"
    assert (target, "user_reported") not in observer.receipts
    assert service.core.notifications.receipts(service.handle, event.id) == before
