"""Absolute launch caps use actual transactional owner/parent bounds."""

from datetime import timedelta

import pytest

from tests.test_build_control_policy_amendment import Rig
from tools.build_control import ControlError


@pytest.fixture
def capped(tmp_path):
    rig = Rig(tmp_path)
    rig.amend()
    rig.batch(count=20, seconds=60, budget="0")
    return rig


def reserve(rig, name, parent=None):
    return rig.current.reserve(
        name,
        label="Owned deadline control",
        paid=False,
        max_cost_usd=None,
        uses_worker=parent is None,
        batch_id="batch",
        timeout_seconds=20,
        metadata={"parent_call_id": parent},
    )


def rows(rig):
    return {row["call_id"]: row for row in rig.current.snapshot()["calls"]}


def test_owner_cap_is_persisted_across_claim_delay_and_reopen(capped):
    r = capped
    cap = r.now + timedelta(seconds=5)
    reserve(r, "owned")
    r.now += timedelta(seconds=2)
    call = r.current.claim_launch("owned", worker_identity="worker", not_after=cap)
    assert call["invocation_bound"]["execution_deadline"] == cap.isoformat()
    reopened = r.control(1)
    assert reopened.snapshot()["calls"][0]["invocation_bound"] == call["invocation_bound"]
    with reopened._store.transaction() as transaction:
        event = next(e for e in transaction.events() if e["kind"] == "launch_claimed")
    assert event["payload"]["deadline_cap"] == {
        "owner_not_after": cap.isoformat(),
        "effective_not_after": cap.isoformat(),
    }


def test_actual_parent_deadline_caps_later_child_claim(capped):
    r = capped
    reserve(r, "parent")
    parent = r.current.claim_launch(
        "parent", worker_identity="worker", not_after=r.now + timedelta(seconds=6)
    )
    reserve(r, "child", "parent")
    r.now += timedelta(seconds=4)
    child = r.current.claim_launch(
        "child", worker_identity="worker", not_after=r.now + timedelta(seconds=40)
    )
    assert (
        child["invocation_bound"]["execution_deadline"]
        == (parent["invocation_bound"]["execution_deadline"])
    )
    with r.current._store.transaction() as transaction:
        event = next(
            e
            for e in transaction.events()
            if e["kind"] == "launch_claimed" and e["call_id"] == "child"
        )
    assert event["payload"]["deadline_cap"]["parent_call_id"] == "parent"
    assert (
        event["payload"]["deadline_cap"]["parent_not_after"]
        == (parent["invocation_bound"]["execution_deadline"])
    )


def test_expired_absolute_cap_leaves_reservation_unlaunched(capped):
    r = capped
    reserve(r, "expired")
    cap = r.now + timedelta(seconds=1)
    r.now = cap
    with pytest.raises(ControlError, match="deadline"):
        r.current.claim_launch("expired", worker_identity="worker", not_after=cap)
    row = rows(r)["expired"]
    assert row["status"] == "reserved"
    assert row["invocation_bound"]["execution_deadline"] is None


@pytest.mark.parametrize("variant", ["missing", "reserved", "foreign", "finished"])
def test_unavailable_parent_cannot_grant_child_deadline(capped, variant):
    r = capped
    if variant != "missing":
        reserve(r, "parent")
        if variant != "reserved":
            r.current.claim_launch(
                "parent", worker_identity="other" if variant == "foreign" else "worker"
            )
        if variant == "finished":
            r.current.record_outcome(
                "parent",
                outcome="failed",
                process_stopped=True,
                evidence_ref="owned non-execution control",
            )
    reserve(r, "child", "parent")
    with pytest.raises(ControlError):
        r.current.claim_launch(
            "child", worker_identity="worker", not_after=r.now + timedelta(seconds=10)
        )
    assert rows(r)["child"]["status"] == "reserved"


def test_explicit_cap_refuses_legacy_unbounded_storage(tmp_path):
    r = Rig(tmp_path)
    r.base.reserve("legacy", label="Legacy control", paid=False, max_cost_usd=None)
    with pytest.raises(ControlError) as error:
        r.base.claim_launch(
            "legacy", worker_identity="worker", not_after=r.now + timedelta(seconds=10)
        )
    assert error.value.code == "invocation_bound_required"
    assert r.base.snapshot()["calls"][0]["status"] == "reserved"
