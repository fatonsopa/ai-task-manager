from dataclasses import replace

import pytest

from ai_task_manager.application.core import Core
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    ApprovalRequired,
    EvidenceRequired,
    IdempotencyConflict,
    InvalidTransition,
)
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.models import (
    RenderedProse,
    SessionHandle,
    TaskProse,
)

from .helpers import Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "fixture.sqlite")


def test_p03_p23_initial_backlog_intent_precedes_provider_and_queue(rig):
    task = rig.task(confirmed=False)
    assert task.issue_id is None and task.observed_status is None
    assert task.desired_status == S.BACKLOG and task.execution_state == "pending_intake"
    operation = rig.core.sync.list_operations(rig.handle)[0]
    assert operation.kind == "create_backlog" and operation.state == "pending"
    with pytest.raises(InvalidTransition):
        rig.reserved(task)
    with pytest.raises(InvalidTransition):
        rig.transition(task, S.TODO, "enqueue")
    task = rig.confirm(task)
    assert task.observed_status == S.BACKLOG and not task.queued_once
    with pytest.raises(InvalidTransition):
        rig.reserved(task)
    queued = rig.transition(task, S.TODO, "enqueue", confirm=False)
    assert queued.desired_status == S.TODO and queued.observed_status == S.BACKLOG
    with pytest.raises(InvalidTransition):
        rig.reserved(queued)
    queued = rig.confirm(queued)
    assert queued.queued_once and queued.observed_status == S.TODO
    rig.reserved(queued)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "title",
        "body",
        "code_literal",
        "writer",
        "reference",
        "style",
        "representation",
    ],
)
def test_p11_exact_content_and_frozen_approval_versions(rig, change):
    prose = TaskProse("Verify result", "Run `check --exact`.")
    ref = rig.approval(prose)
    approval = rig.proofs.approvals[ref]
    if change == "missing":
        ref = None
    elif change == "title":
        prose = replace(prose, title="Changed title")
    elif change == "body":
        prose = replace(prose, body="Changed wording.")
    elif change == "code_literal":
        prose = replace(prose, body="Run `check --guess`.")
    elif change in {"writer", "reference", "style"}:
        rig.proofs.approvals[ref] = replace(
            approval, policy=replace(approval.policy, **{f"{change}_hash": "stale"})
        )
    else:
        rig.proofs.approvals[ref] = replace(approval, representation="other")
    with pytest.raises(ApprovalRequired):
        rig.core.intake.submit(rig.handle, prose, ref, rig.key())
    assert rig.core.list_tasks(rig.handle) == []


def test_p11_no_reviewed_boolean_or_default_self_approval(rig):
    prose = TaskProse("Verify result", "Run `check`.")
    with pytest.raises(TypeError):
        rig.core.intake.submit(rig.handle, prose, None, rig.key(), reviewed=True)
    default = Core(rig.db, rig.policy)
    with pytest.raises(AccessDenied):
        default.register_session("model-asserted-user")
    with pytest.raises(ApprovalRequired):
        default.intake.submit(rig.handle, prose, "reviewed=True", rig.key())


class ProviderFixtureRenderer:
    def render(self, prose):
        return RenderedProse(
            prose.title,
            prose.body.replace("- ", "* ").replace("\n\n* ", "\n* "),
            "provider-fixture-v1",
        )


def test_p12_supported_provider_encoding_is_approved_before_publication(tmp_path):
    rig = Rig(tmp_path / "fixture.sqlite", renderer=ProviderFixtureRenderer())
    prose = TaskProse("Verify output", "- run `check`\n\n- retain output")
    ref = rig.approval(prose)
    task = rig.core.intake.submit(rig.handle, prose, ref, rig.key())
    assert task.body == "* run `check`\n* retain output"
    for changed in (
        "- run `other`\n\n- retain output",
        "- run `check`\n\n- discard output",
    ):
        with pytest.raises(ApprovalRequired):
            rig.core.intake.submit(
                rig.handle, replace(prose, body=changed), ref, rig.key()
            )


@pytest.mark.parametrize(
    "scope_field", ["provider", "workspace", "principal", "project", "logical_session"]
)
def test_p07_every_scope_dimension_blocks_task_event_and_attempt_access(
    rig, scope_field
):
    task, attempt = rig.reserved()
    other_scope = replace(rig.scope, **{scope_field: f"other-{scope_field}"})
    other = rig.session(other_scope)
    event = rig.core.notifications.list_events(rig.handle)[0]
    assert (
        rig.core.list_tasks(other) == []
        and rig.core.notifications.list_events(other) == []
    )
    for method, args in [
        (rig.core.get_task, (task.id,)),
        (rig.core.get_attempt, (attempt.id,)),
        (rig.core.notifications.receipts, (event.id,)),
    ]:
        with pytest.raises(AccessDenied):
            method(other, *args)
    with pytest.raises(AccessDenied):
        rig.core.transitions.transition(
            other,
            task.id,
            S.BACKLOG,
            "dequeue",
            task.version,
            task.decision_revision,
            rig.decision(task, S.BACKLOG),
            {},
            rig.key(),
        )


def test_p07_forged_and_revoked_capabilities(rig):
    for handle in (None, SessionHandle("guess")):
        with pytest.raises(AccessDenied):
            rig.core.list_tasks(handle)
    rig.core.revoke_session(rig.handle)
    with pytest.raises(AccessDenied):
        rig.core.list_tasks(rig.handle)


def test_p10_operation_identity_scopes_payload_and_original_result(rig):
    prose = TaskProse("Verify output", "Run `check`.")
    ref, key = rig.approval(prose), rig.key()
    first = rig.core.intake.submit(rig.handle, prose, ref, key)
    assert rig.core.intake.submit(rig.handle, prose, ref, key) == first
    changed = replace(prose, title="Other task")
    with pytest.raises(IdempotencyConflict):
        rig.core.intake.submit(rig.handle, changed, rig.approval(changed), key)
    other = rig.session(replace(rig.scope, logical_session="other"))
    with pytest.raises(IdempotencyConflict):
        rig.core.intake.submit(other, prose, ref, key)
    assert len(rig.core.list_tasks(rig.handle)) == 1


def test_p25_known_session_cannot_mint_authority_without_trusted_resume(rig):
    ref = rig.key()
    rig.proofs.principals[ref] = rig.scope
    sid, _ = rig.core.session_binding(rig.handle)
    with pytest.raises(EvidenceRequired):
        rig.core.register_session(ref)
    for proof in [
        rig.proof("session_resume", "other"),
        rig.proof("session_resume", sid, scope=replace(rig.scope, principal="other")),
        rig.proof("session_resume", sid, expires_at=rig.now),
    ]:
        with pytest.raises(EvidenceRequired):
            rig.core.register_session(ref, proof)
    resume_ref = rig.proof("session_resume", sid)
    resumed = rig.core.register_session(ref, resume_ref)
    assert rig.core.session_binding(resumed)[0] == sid
    with pytest.raises(AccessDenied):
        rig.core.list_tasks(rig.handle)
    with pytest.raises(EvidenceRequired):
        rig.core.register_session(ref, resume_ref)


def test_p11_revised_prose_needs_fresh_exact_approval_and_invalidates_old_completion(
    rig,
):
    task, attempt = rig.running()
    task, _ = rig.settle(task, attempt)
    task = rig.complete(task)
    prose = TaskProse(
        "Verify expanded scope",
        "Run `check --new-scope`.",
        required_work=("original", "additional"),
    )
    with pytest.raises(ApprovalRequired):
        rig.core.intake.revise(
            rig.handle,
            task.id,
            prose,
            task.approval_ref,
            task.version,
            task.decision_revision,
            rig.decision(task, S.IN_PROGRESS),
            rig.key(),
        )
    revised = rig.core.intake.revise(
        rig.handle,
        task.id,
        prose,
        rig.approval(prose),
        task.version,
        task.decision_revision,
        rig.decision(task, S.IN_PROGRESS),
        rig.key(),
    )
    assert revised.completed_work == () and revised.required_work == (
        "original",
        "additional",
    )
    assert revised.sync_state == "pending" and revised.prose_digest != task.prose_digest
    assert rig.core.sync.list_operations(rig.handle)[-1].kind == "update_prose"


@pytest.mark.parametrize("changed_field", ["title", "body", "representation"])
def test_p11_provider_readback_must_match_exact_approved_representation(
    rig, changed_field
):
    task = rig.task(confirmed=False)
    operation = rig.core.sync.list_operations(rig.handle)[0]
    payload = {
        "operation_id": operation.id,
        "status": S.BACKLOG,
        "prose_digest": task.prose_digest,
        "title": task.title,
        "body": task.body,
        "representation": task.representation,
        "issue_id": "provider-canonical-fixture-id",
    }
    payload[changed_field] = "changed value"
    proof = rig.proof("provider_status", task.id, payload)
    with pytest.raises(EvidenceRequired):
        rig.core.intake.confirm_status(
            rig.handle, task.id, task.version, task.decision_revision, proof, rig.key()
        )
    assert rig.current(task).observed_status is None
