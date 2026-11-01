from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import CommentCreateCapability
from ai_task_manager.application.linear_sync.progress import (
    ProgressApprovalContext,
    ProgressPublisher,
    progress_prose,
)

from ai_task_manager.domains.work.errors import ApprovalRequired, IdempotencyConflict
from tests.linear.helpers import LinearRig, ProviderFixture, uid


class ScopedProgressApprovals:
    def __init__(self, rig, task):
        self.rig, self.task_id, self.task_digest, self.scope = (
            rig,
            task.id,
            task.prose_digest,
            rig.scope,
        )
        self.refs = {}

    def approve(self, prose):
        fixture_ref = self.rig.approval(prose)
        ref = f"writing-progress:{uuid4()}"
        self.refs[ref] = self.rig.proofs.approvals[fixture_ref]
        return ref

    def verify_approval(self, ref):
        task = self.rig.core.get_task(self.rig.handle, self.task_id)
        if (
            ref not in self.refs
            or task.prose_digest != self.task_digest
            or self.rig.scope != self.scope
        ):
            raise ApprovalRequired("Contextual progress approval does not match")
        return self.refs[ref]


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def setup(rig):
    task = rig.task()
    approvals = ScopedProgressApprovals(rig, task)
    publisher = ProgressPublisher(
        rig.core,
        rig.client,
        rig.journal,
        approval_policy=rig.policy,
        approvals=approvals,
        approval_context=ProgressApprovalContext(rig.scope, task.id, task.prose_digest),
        clock=lambda: rig.now,
    )
    prose = progress_prose("* Verified `check --scope=session`.\n* The provider update is pending.")
    return task, publisher, prose, approvals.approve(prose), approvals


@pytest.mark.parametrize("change", ["missing", "body", "literal", "title", "writer", "purpose"])
def test_lp01_unreviewed_changed_or_wrong_purpose_progress_is_rejected_before_effect(rig, change):
    task, publisher, prose, ref, approvals = setup(rig)
    if change == "missing":
        ref = None
    elif change == "body":
        prose = replace(prose, body="Different claim.")
    elif change == "literal":
        prose = replace(prose, body=prose.body.replace("--scope=session", "--all"))
    elif change == "title":
        prose = replace(prose, title="Another purpose")
    elif change == "writer":
        approvals.refs[ref] = replace(
            approvals.refs[ref], policy=replace(rig.policy, writer_hash="stale")
        )
    else:
        prose = replace(prose, code_bearing=True)
    with pytest.raises(ApprovalRequired):
        publisher.submit(rig.handle, task.id, prose, ref, rig.key())
    assert not rig.provider.comments


def test_lp02_lp03_durable_intent_then_exact_actual_http_comment_readback(rig):
    task, publisher, prose, ref, _ = setup(rig)
    key = rig.key()
    row = publisher.submit(rig.handle, task.id, prose, ref, key)
    assert row["state"] == "pending" and not rig.provider.comments
    assert publisher.submit(rig.handle, task.id, prose, ref, key)["id"] == row["id"]
    before = rig.current(task)
    receipts = rig.core.notifications.receipts(
        rig.handle, rig.core.notifications.list_events(rig.handle)[0].id
    )
    result = publisher.publish_pending(rig.handle, row["id"])
    assert result["state"] == "confirmed" and len(rig.provider.comments) == 1
    assert (
        result["created_at"] <= result["sent_at"] <= result["observed_at"] <= result["confirmed_at"]
    )
    assert rig.provider.comments[result["comment_id"]]["body"] == prose.body
    assert publisher.publish(rig.handle, task.id, prose, ref, key)["id"] == row["id"]
    assert len(rig.provider.comments) == 1 and rig.current(task) == before
    assert (
        rig.core.notifications.list_events(rig.handle)[-1].kind
        == "provider_progress_publication_confirmed"
    )
    assert rig.core.notifications.receipts(
        rig.handle, rig.core.notifications.list_events(rig.handle)[-1].id
    ) == {"queued": "local-transaction"}
    assert (
        rig.core.notifications.receipts(
            rig.handle, rig.core.notifications.list_events(rig.handle)[0].id
        )
        == receipts
    )


def test_lp04_progress_owner_or_provider_scope_change_blocks_before_publication(rig):
    task, publisher, prose, ref, _ = setup(rig)
    row = publisher.submit(rig.handle, task.id, prose, ref, rig.key())
    rig.provider.edit(task.issue_id, assignee={"id": uid(900)})
    assert publisher.publish_pending(rig.handle, row["id"])["state"] == "failed"
    assert not rig.provider.comments


def test_lp05_exact_comment_code_literal_readback_mismatch_stays_uncertain(rig):
    task, publisher, prose, ref, _ = setup(rig)

    def modify(request, result):
        if "mutation CreateComment" in request["query"]:
            comment = result["commentCreate"]["comment"]
            rig.provider.comments[comment["id"]]["body"] = "Changed `--all` literal."

    rig.provider.hook = modify
    row = publisher.publish(rig.handle, task.id, prose, ref, rig.key())
    assert row["state"] == "uncertain"
    assert publisher.publish_pending(rig.handle, row["id"])["state"] == "uncertain"
    assert len(rig.provider.comments) == 1


def test_lp06_readback_failure_and_restart_recover_known_comment_without_duplicate(rig):
    task, publisher, prose, ref, approvals = setup(rig)

    def unavailable(request, result):
        if "mutation CreateComment" in request["query"]:
            rig.provider.fail_next = (503, b"unavailable")

    rig.provider.hook = unavailable
    row = publisher.publish(rig.handle, task.id, prose, ref, rig.key())
    assert row["state"] == "uncertain" and row["comment_id"]
    rig.provider.hook = None
    restarted = ProgressPublisher(
        rig.core,
        rig.client,
        rig.journal,
        approval_policy=rig.policy,
        approvals=approvals,
        approval_context=ProgressApprovalContext(rig.scope, task.id, task.prose_digest),
        clock=lambda: rig.now,
    )
    assert restarted.recover(rig.handle, row["id"])["state"] == "confirmed"
    assert len(rig.provider.comments) == 1


@pytest.mark.parametrize("candidate_enabled", [False, True])
def test_lp07_lost_reply_never_blindly_reposts_comment(rig, monkeypatch, candidate_enabled):
    task, publisher, prose, ref, _ = setup(rig)
    if candidate_enabled:
        rig.client.comment_capability = CommentCreateCapability.from_introspection(
            {
                "name": "CommentCreateInput",
                "inputFields": [
                    {"name": name, "type": {"kind": "SCALAR", "name": "String", "ofType": None}}
                    for name in ("id", "issueId", "body")
                ],
            }
        )
    original = rig.client.create_comment

    def lose(issue_id, body, candidate_id=None):
        original(issue_id, body, candidate_id)
        raise LinearError("transport_failure", uncertain=True)

    monkeypatch.setattr(rig.client, "create_comment", lose)
    row = publisher.publish(rig.handle, task.id, prose, ref, rig.key())
    assert row["state"] == "uncertain" and len(rig.provider.comments) == 1
    if candidate_enabled:
        assert publisher.recover(rig.handle, row["id"])["state"] == "confirmed"
    else:
        with pytest.raises(LinearError, match="comment_identity_uncertain"):
            publisher.recover(rig.handle, row["id"])
    with pytest.raises(LinearError, match="progress_effect_not_proven_absent"):
        publisher.retry_unsent(rig.handle, row["id"])
    assert len(rig.provider.comments) == 1


def test_lp08_concurrent_publishers_claim_one_comment_intent(rig):
    task, publisher, prose, ref, _ = setup(rig)
    row = publisher.submit(rig.handle, task.id, prose, ref, rig.key())
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(lambda _: publisher.publish_pending(rig.handle, row["id"]), range(2))
        )
    assert any(item["state"] == "confirmed" for item in outcomes)
    assert len(rig.provider.comments) == 1


def test_lp09_context_change_and_default_approval_gate_prevent_replay(rig):
    task, publisher, prose, ref, approvals = setup(rig)
    default = ProgressPublisher(rig.core, rig.client, rig.journal, approval_policy=rig.policy)
    with pytest.raises(ApprovalRequired):
        default.submit(rig.handle, task.id, prose, ref, rig.key())
    row = publisher.submit(rig.handle, task.id, prose, ref, rig.key())
    changed = replace(prose, title="Revised task", code_bearing=True, required_work=("new",))
    rig.core.intake.revise(
        rig.handle,
        task.id,
        changed,
        rig.approval(changed),
        task.version,
        task.decision_revision,
        rig.decision(task, "Backlog"),
        rig.key(),
    )
    with pytest.raises(ApprovalRequired):
        publisher.publish_pending(rig.handle, row["id"])
    assert not rig.provider.comments


def test_lp02_changed_payload_cannot_reuse_publication_operation(rig):
    task, publisher, prose, ref, approvals = setup(rig)
    key = rig.key()
    publisher.submit(rig.handle, task.id, prose, ref, key)
    changed = progress_prose("Another reviewed update.")
    with pytest.raises(IdempotencyConflict):
        publisher.submit(rig.handle, task.id, changed, approvals.approve(changed), key)


def test_lp09_progress_context_cannot_cross_identical_task_prose_or_session(rig):
    task, publisher, prose, ref, approvals = setup(rig)
    other = rig.task()
    assert task.prose_digest == other.prose_digest
    with pytest.raises(ApprovalRequired):
        publisher.submit(rig.handle, other.id, prose, ref, rig.key())
    assert not rig.provider.comments
