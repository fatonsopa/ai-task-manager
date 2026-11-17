"""Connected real local retirement and interrupted acknowledgement controls."""

import pytest

from ai_task_manager.adapters.resources.release import DeliveryReleasePolicy
from ai_task_manager.supervisor.models import RuntimeRefused

from .test_cleanup_helpers import stopped


@pytest.mark.parametrize("kind", ["worktree", "database"])
def test_original_removal_intent_reconciles_lost_reply_without_resend(rr, monkeypatch, kind):
    dispatch = rr.start(rr.rig.queued(code=False))
    rr.admit(dispatch)
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    pair = dispatch.resources
    rr.resources.release_policy = DeliveryReleasePolicy(rr.repo)
    provider = rr.resources.git if kind == "worktree" else rr.resources.databases
    original = provider.remove
    calls = []

    def lose_reply(path, receipt):
        calls.append(str(path))
        original(path, receipt)
        raise ConnectionError("Removal reply deliberately lost")

    monkeypatch.setattr(provider, "remove", lose_reply)
    with pytest.raises(ConnectionError):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    monkeypatch.setattr(provider, "remove", original)
    assert rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id) == {
        "worktree": "removed",
        "database": "removed",
    }
    assert rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id) == {
        "worktree": "removed",
        "database": "removed",
    }
    assert len(calls) == 1
    assert not pair.worktree.exists() and not pair.database.exists()
    assert rr.journal.observations(pair.attempt_id, kind + "_removed")[-1][
        "reconciled_original_intent"
    ]


def test_retired_path_replacement_is_never_removed(rr):
    pair = stopped(rr)
    rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    pair.database.write_text("foreign replacement")
    with pytest.raises(RuntimeRefused, match="new object"):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    assert pair.database.read_text() == "foreign replacement"


def test_current_consumer_prevents_retirement(rr):
    pair = stopped(rr)
    with rr.resources.use(pair):
        with pytest.raises(RuntimeRefused, match="active resource consumer"):
            rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    assert pair.worktree.exists() and pair.database.exists()


def test_missing_removal_intent_does_not_convert_absence_to_success(rr):
    pair = stopped(rr)
    pair.database.unlink()
    with pytest.raises((RuntimeRefused, FileNotFoundError)):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    assert pair.worktree.exists()


def test_delivered_merged_resource_with_foreign_current_ownership_is_protected(rr):
    from dataclasses import replace

    pair = stopped(rr)
    task = rr.rig.core.get_task(rr.rig.handle, rr.journal.attempt(pair.attempt_id)["task_id"])

    class CurrentForeignProvider:
        def get_task(self, handle, task_id):
            return replace(task, delivered_once=True, sync_state="foreign")

    fact = rr.resources.verify(pair)
    with pytest.raises(RuntimeRefused, match="foreign or unconfirmed"):
        DeliveryReleasePolicy(rr.repo).authorize(
            CurrentForeignProvider(),
            rr.rig.handle,
            rr.rig.core.get_attempt(rr.rig.handle, pair.attempt_id),
            fact,
        )
    assert pair.worktree.exists() and pair.database.exists()
