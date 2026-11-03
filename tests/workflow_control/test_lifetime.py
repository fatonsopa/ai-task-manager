"""Stop outcomes are observed once; lost receipts never justify another stop."""

from ai_task_manager.application.workflows.service import WorkflowService
from tests.workflows.helpers import WorkflowFixture


def test_WE13_orderly_close_stops_and_repeated_close_observes(tmp_path):
    fixture = WorkflowFixture(tmp_path, max_steps=1)
    service = WorkflowService(fixture.driver)
    try:
        assert service.resume(fixture.ref)["data"]["state"] == "checkpoint"
        first = service.close()
        assert first[0]["state"] == "paused"
        before = fixture.rr.journal.observations(
            fixture.store.read(fixture.ref, fixture.rig.scope)["attempt_id"],
            "runtime_outcome",
        )
        assert service.close()[0]["state"] == "paused"
        after = fixture.rr.journal.observations(
            fixture.store.read(fixture.ref, fixture.rig.scope)["attempt_id"], "runtime_outcome"
        )
        assert after == before
        saved = fixture.store.read(fixture.ref, fixture.rig.scope)["steps"]["owner-stop"]
        assert saved["payload"]["operation_key"] == fixture.ref + ":owner-stop"
        assert saved["result"]["state"] == "paused"
    finally:
        fixture.close()


def test_WE13_lost_stop_reply_reconciles_core_without_another_signal(tmp_path):
    fixture = WorkflowFixture(tmp_path, max_steps=1)
    service = WorkflowService(fixture.driver)
    try:
        service.resume(fixture.ref)
        original = fixture.runtime.stop
        calls = []

        def lost(*args):
            calls.append(args[-1])
            original(*args)
            raise OSError("authored lost stop reply")

        fixture.runtime.stop = lost
        assert service.close()[0]["state"] == "uncertain"
        assert service.close()[0]["state"] == "paused"
        assert len(calls) == 1
        assert service.close()[0]["state"] == "paused"
        assert len(calls) == 1
    finally:
        fixture.close()


def test_WE13_missing_stop_owner_retains_obligation(tmp_path):
    fixture = WorkflowFixture(tmp_path, max_steps=1)
    service = WorkflowService(fixture.driver)
    try:
        service.resume(fixture.ref)
        row = fixture.store.read(fixture.ref, fixture.rig.scope)
        fixture.runtime.dispatches.clear()
        result = service.close()
        assert result[0]["state"] == "uncertain"
        assert (
            fixture.rig.core.get_attempt(fixture.rig.handle, row["attempt_id"]).state == "running"
        )
        assert "owner-stop" not in fixture.store.read(fixture.ref, fixture.rig.scope)["steps"]
    finally:
        fixture.close()
