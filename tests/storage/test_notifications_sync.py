from dataclasses import replace

import pytest

from ai_task_manager.domains.work.errors import (
    AccessDenied,
    EvidenceRequired,
    IdempotencyConflict,
    ValidationError,
)
from ai_task_manager.domains.work.models import ReceiptStage
from ai_task_manager.storage.work import fingerprint
from tests.domains.helpers import Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "fixture.sqlite")


def test_p17_queued_delivered_ack_and_user_report_require_separate_ordered_receipts(
    rig,
):
    rig.task()
    event = rig.core.notifications.list_events(rig.handle)[0]
    sid, _ = rig.core.session_binding(rig.handle)
    assert set(rig.core.notifications.receipts(rig.handle, event.id)) == {"queued"}
    ai = rig.proof("notification_ai_acknowledged", event.id, {"target_session_id": sid})
    with pytest.raises(ValidationError):
        rig.core.notifications.acknowledge(
            rig.handle, event.id, "ai_acknowledged", ai, rig.key()
        )
    for stage in (
        ReceiptStage.DELIVERED,
        ReceiptStage.AI_ACKNOWLEDGED,
        ReceiptStage.USER_REPORTED,
    ):
        proof = rig.proof(f"notification_{stage}", event.id, {"target_session_id": sid})
        rig.core.notifications.acknowledge(
            rig.handle, event.id, stage, proof, rig.key()
        )
    assert set(rig.core.notifications.receipts(rig.handle, event.id)) == {
        stage.value for stage in ReceiptStage
    }


def test_p17_wrong_target_proof_does_not_imply_delivery(rig):
    rig.task()
    event = rig.core.notifications.list_events(rig.handle)[0]
    proof = rig.proof(
        "notification_delivered", event.id, {"target_session_id": "other-session"}
    )
    with pytest.raises(EvidenceRequired):
        rig.core.notifications.acknowledge(
            rig.handle, event.id, "delivered", proof, rig.key()
        )
    assert set(rig.core.notifications.receipts(rig.handle, event.id)) == {"queued"}


def test_p18_cursors_are_scoped_and_do_not_acknowledge_ai(rig):
    rig.task()
    events = rig.core.notifications.list_events(rig.handle)
    other = rig.session(replace(rig.scope, logical_session="session-b"))
    rig.core.notifications.advance_cursor(rig.handle, events[-1].sequence)
    assert rig.core.notifications.cursor(other) == 0
    assert rig.core.notifications.list_events(other) == []
    assert set(rig.core.notifications.receipts(rig.handle, events[0].id)) == {"queued"}
    with pytest.raises(ValidationError):
        rig.core.notifications.advance_cursor(other, events[-1].sequence)
    with pytest.raises(AccessDenied):
        rig.core.notifications.receipts(other, events[0].id)


def test_p19_claims_expire_to_uncertainty_without_blind_retry(rig):
    task = rig.task(confirmed=False)
    claim = rig.core.sync.claim(rig.handle, "adapter-a", lease_seconds=10)
    assert claim.attempt_count == 1 and claim.state == "leased"
    assert rig.core.sync.claim(rig.handle, "adapter-b") is None
    rig.now += 11
    assert rig.core.sync.claim(rig.handle, "adapter-b") is None
    operation = rig.core.sync.list_operations(rig.handle)[0]
    assert operation.state == "uncertain"
    assert rig.current(task).issue_id is None
    with pytest.raises(ValidationError):
        rig.core.sync.record_outcome(
            rig.handle, operation.id, "adapter-a", 1, "failed", "late response"
        )
    proof = rig.proof(
        "outbound_absent", operation.id,
        {"safe_to_retry": True, "attempt_count": operation.attempt_count},
    )
    rig.core.sync.reconcile_retry(rig.handle, operation.id, proof)
    next_claim = rig.core.sync.claim(rig.handle, "adapter-b")
    assert next_claim.id == operation.id and next_claim.attempt_count == 2
    rig.core.sync.record_outcome(
        rig.handle, next_claim.id, "adapter-b", 2, "failed", "authored refusal before send"
    )
    with pytest.raises(EvidenceRequired):
        rig.core.sync.reconcile_retry(rig.handle, operation.id, proof)
    with rig.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT outcome FROM outbound_attempts WHERE operation_id=?",
                (operation.id,),
            ).fetchone()[0]
            == "uncertain"
        )


def test_p19_failure_retained_without_fabricated_confirmation(rig):
    rig.task(confirmed=False)
    operation = rig.core.sync.claim(rig.handle, "adapter")
    rig.core.sync.record_outcome(
        rig.handle,
        operation.id,
        "adapter",
        operation.attempt_count,
        "failed",
        "provider rejected scope",
    )
    assert rig.core.sync.list_operations(rig.handle)[0].state == "failed"
    assert rig.core.sync.claim(rig.handle, "adapter") is None
    with pytest.raises(EvidenceRequired):
        rig.core.sync.reconcile_retry(rig.handle, operation.id, None)


def test_p20_authenticated_inbound_dedup_is_durable_but_not_automatically_applied(rig):
    payload = {"issue": "TST:fixture", "status": "On Hold", "revision": 11}
    ref = rig.proof(
        "provider_ingress", "delivery-1", {"payload_digest": fingerprint(payload)}
    )
    assert rig.core.sync.ingest(rig.handle, "delivery-1", payload, ref)
    assert not rig.core.sync.ingest(rig.handle, "delivery-1", payload, ref)
    rows = rig.core.sync.inbound(rig.handle)
    assert len(rows) == 1 and rows[0]["state"] == "received"
    changed = {**payload, "status": "Done"}
    other = rig.proof(
        "provider_ingress", "delivery-1", {"payload_digest": fingerprint(changed)}
    )
    with pytest.raises(IdempotencyConflict):
        rig.core.sync.ingest(rig.handle, "delivery-1", changed, other)
    with pytest.raises(EvidenceRequired):
        rig.core.sync.ingest(rig.handle, "delivery-2", payload, ref)
    applied = rig.proof(
        "ingress_applied", "delivery-1", {"payload_digest": fingerprint(payload)}
    )
    rig.core.sync.mark_applied(rig.handle, "delivery-1", applied)
    assert rig.core.sync.inbound(rig.handle)[0]["state"] == "applied"
