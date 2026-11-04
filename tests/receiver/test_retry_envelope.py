"""Real loopback admission and current enrollment; no external provider effects."""

import json
import time

import pytest

from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from tests.linear.helpers import STATES, uid
from tests.receiver.helpers import ROUTE, Fixture, request, wait_for
from tests.receiver.test_authority_boundary import revoke


@pytest.mark.parametrize("authority", ["current", "revoked", "expired"])
def test_re12_listener_rechecks_authority_for_previously_accepted_retry(tmp_path, authority):
    fixture = Fixture(tmp_path)
    receiver = None
    try:
        service = fixture.load()
        receiver = InstalledReceiver(service, fixture.config)
        receiver.start()
        fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        event = fixture.provider.event(fixture.task.issue_id, timestamp=int(time.time() * 1000))
        delivery = uid(178)
        assert request(receiver.http.address, ROUTE, *fixture.rig.signed(event, delivery))[0] == 200
        wait_for(lambda: service.inbound.journal.deliveries(service.scope)[0]["state"] == "applied")
        before = service.core.get_task(service.handle, fixture.task.id)
        rows = service.inbound.journal.deliveries(service.scope)
        events = service.core.notifications.list_events(service.handle)
        if authority == "revoked":
            revoke(receiver)
        elif authority == "expired":
            receiver.observations.journal.clock = lambda: time.time() + 3600
        retry = {**event, "webhookTimestamp": event["webhookTimestamp"] + 1}
        assert request(receiver.http.address, ROUTE, *fixture.rig.signed(retry, delivery))[0] == (
            200 if authority == "current" else 503
        )
        assert service.inbound.journal.deliveries(service.scope) == rows
        assert service.core.get_task(service.handle, fixture.task.id) == before
        assert service.core.notifications.list_events(service.handle) == events
    finally:
        if receiver:
            cleanup = receiver.stop()
            (tmp_path / "receiver-cleanup.json").write_text(json.dumps(cleanup))
            assert cleanup["stopped"] and cleanup["ownership_released"]
            assert receiver.worker is None or not receiver.worker.is_alive()
            if authority == "current":
                assert cleanup["failure"] is None
        fixture.close()
        assert not fixture.provider.thread.is_alive()
