"""Verified later human holds share policy with explicit release and work admission."""

from dataclasses import replace

import pytest

from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition, StaleDecision
from ai_task_manager.domains.work.models import IssueStatus as S
from tests.domains.helpers import Rig
from tests.domains.test_external_supersession import observation
from tests.domains.test_review_delivery import review


def settle(rig, task, target=S.BACKLOG, revision="2026-11-01T00:00:02.000000Z", **facts):
    ref = observation(rig, task, status=target, revision=revision, **facts)
    observed = rig.core.external.observe(
        rig.handle, task.id, task.version, task.decision_revision, ref, rig.key()
    )
    task = rig.current(task)
    result = rig.core.external.settle(
        rig.handle,
        task.id,
        observed["observation_id"],
        task.version,
        task.decision_revision,
        "enqueue" if target == S.TODO else "dequeue",
        {},
        rig.key(),
    )
    return result, ref


def test_hb02_held_backlog_refuses_queue_start_and_stale_release_after_restart(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.queued()
    held, _ = settle(rig, task)
    assert held.human_hold and held.decision_revision == 1
    before = rig.core.sync.list_operations(rig.handle)
    rig.core = rig.new_core()
    assert rig.current(held) == held
    for actor in ["automation", "human"]:
        with pytest.raises(StaleDecision, match="Human hold"):
            rig.transition(held, S.TODO, "enqueue", actor=actor)
        with pytest.raises(StaleDecision, match="Human hold"):
            rig.core.execution.reserve(
                rig.handle,
                held.id,
                "host",
                held.version,
                held.decision_revision,
                rig.decision(held, S.IN_PROGRESS, actor=actor),
                rig.key(),
            )
    for fault in ["missing", "scope", "revision"]:
        ref = (
            None
            if fault == "missing"
            else rig.proof(
                "human_decision",
                held.id,
                {
                    "previous_revision": 0 if fault == "revision" else held.decision_revision,
                    "hold": False,
                },
                scope=replace(rig.scope, principal="another-human")
                if fault == "scope"
                else rig.scope,
            )
        )
        with pytest.raises((EvidenceRequired, StaleDecision)):
            rig.core.decisions.record(rig.handle, held.id, held.version, ref, rig.key())
    assert rig.current(held) == held and rig.core.sync.list_operations(rig.handle) == before
    with rig.db.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 0
    ref = rig.proof(
        "human_decision", held.id, {"previous_revision": held.decision_revision, "hold": False}
    )
    key = rig.key()
    released = rig.core.decisions.record(rig.handle, held.id, held.version, ref, key)
    assert rig.core.decisions.record(rig.handle, held.id, held.version, ref, key) == released
    assert released.decision_revision == 2 and not released.human_hold
    queued = rig.transition(released, S.TODO, "enqueue")
    assert queued.observed_status == S.TODO and not queued.human_hold
    assert len(rig.core.sync.list_operations(rig.handle)) == len(before) + 1


def test_hb03_same_status_new_human_decision_is_held_but_duplicate_and_older_are_noops(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    initial = rig.task()
    assert not initial.human_hold and initial.decision_revision == 0
    held, ref = settle(rig, initial)
    assert held.observed_status == S.BACKLOG and held.human_hold
    assert held.decision_revision == 1
    for proof, expected in [
        (ref, "duplicate"),
        (observation(rig, held, revision="2026-11-01T00:00:01.000000Z"), "stale"),
    ]:
        result = rig.core.external.observe(
            rig.handle, held.id, held.version, held.decision_revision, proof, rig.key()
        )
        assert result["state"] == expected and rig.current(held) == held
    newer, _ = settle(rig, held, revision="2026-11-01T00:00:03.000000Z")
    assert newer.human_hold and newer.decision_revision == 2
    queued, _ = settle(rig, newer, S.TODO, revision="2026-11-01T00:00:04.000000Z")
    assert queued.observed_status == S.TODO and queued.decision_revision == 3
    assert not queued.human_hold


@pytest.mark.parametrize("target", list(S))
def test_hb04_verified_review_destination_keeps_its_existing_guards(tmp_path, target):
    rig = Rig(tmp_path / "core.sqlite")
    task = review(rig)
    ref = observation(rig, task, status=target)
    observed = rig.core.external.observe(
        rig.handle, task.id, task.version, task.decision_revision, ref, rig.key()
    )
    current = rig.current(task)
    proofs = rig.delivery(current)
    proofs["duplicate_target"] = rig.proof(
        "duplicate_target", current.id, {"canonical_issue_id": "TST:canonical"}
    )
    args = (
        rig.handle,
        current.id,
        observed["observation_id"],
        current.version,
        current.decision_revision,
        "review_decision",
        proofs,
        rig.key(),
    )
    if target == S.IN_PROGRESS:
        with pytest.raises(InvalidTransition, match="worker startup"):
            rig.core.external.settle(*args)
        assert rig.current(current) == current
    else:
        final = rig.core.external.settle(*args)
        assert final.observed_status == target
        assert final.human_hold is (target in {S.BACKLOG, S.ON_HOLD})
        assert final.decision_revision == 1


@pytest.mark.parametrize("actor, authorized", [("integration", True), ("human", False)])
def test_hb05_system_or_unauthorized_observation_cannot_release_or_mint_human_decision(
    tmp_path, actor, authorized
):
    rig = Rig(tmp_path / "core.sqlite")
    held, _ = settle(rig, rig.task())
    ref = observation(
        rig,
        held,
        status=S.TODO,
        revision="2026-11-01T00:00:03.000000Z",
        actor_kind=actor,
        actor_authorized=authorized,
    )
    observed = rig.core.external.observe(
        rig.handle, held.id, held.version, held.decision_revision, ref, rig.key()
    )
    current = rig.current(held)
    assert current.human_hold and current.decision_revision == held.decision_revision
    with pytest.raises(EvidenceRequired, match="authorized human"):
        rig.core.external.settle(
            rig.handle,
            current.id,
            observed["observation_id"],
            current.version,
            current.decision_revision,
            "enqueue",
            {},
            rig.key(),
        )
    assert rig.current(current) == current


@pytest.mark.parametrize("target", [S.BACKLOG, S.TODO])
def test_hb06_assignment_removal_still_retains_hold(tmp_path, target):
    rig = Rig(tmp_path / "core.sqlite")
    final, _ = settle(rig, rig.queued(), target, assigned_to_owner=False)
    assert final.human_hold and final.decision_revision == 1
