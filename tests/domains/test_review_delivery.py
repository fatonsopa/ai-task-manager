import pytest

from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition
from ai_task_manager.domains.work.models import IssueStatus as S

from .helpers import Rig


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "fixture.sqlite")


def review(rig, code=True):
    task, attempt = rig.running(rig.queued(code=code))
    task, _ = rig.settle(task, attempt)
    task = rig.complete(task)
    return rig.transition(task, S.IN_REVIEW, "request_review")


@pytest.mark.parametrize("target", list(S))
def test_p02_every_review_exit_is_usable_through_guarded_application_api(rig, target):
    task = review(rig)
    if target == S.IN_PROGRESS:
        task, attempt = rig.running(task)
        assert task.observed_status == target and attempt.state == "running"
    else:
        proofs = rig.delivery(task)
        proofs["duplicate_target"] = rig.proof(
            "duplicate_target", task.id, {"canonical_issue_id": "TST:canonical"}
        )
        task = rig.transition(task, target, "review_decision", proofs)
        assert task.observed_status == target


@pytest.mark.parametrize("target", list(S))
def test_p02_review_exit_requires_explicit_human_authority(rig, target):
    task = review(rig)
    with pytest.raises(EvidenceRequired):
        if target == S.IN_PROGRESS:
            rig.core.execution.reserve(
                rig.handle,
                task.id,
                "host",
                task.version,
                task.decision_revision,
                rig.decision(task, target, actor="automation"),
                rig.key(),
            )
        else:
            rig.transition(
                task, target, "review_decision", rig.delivery(task), actor="automation"
            )


def test_p06_verified_noncode_can_complete_without_deployment(rig):
    task, attempt = rig.running(rig.queued(code=False))
    task, _ = rig.settle(task, attempt)
    task = rig.complete(task)
    proof = rig.delivery(task)
    with pytest.raises(InvalidTransition):
        rig.transition(task, S.DONE, "non_code_complete")
    task = rig.transition(
        task,
        S.DONE,
        "non_code_complete",
        {"non_code_completion": proof["non_code_completion"]},
    )
    assert task.observed_status == S.DONE


def test_p06_cancel_and_duplicate_require_reason_and_canonical_link(rig):
    task = rig.task()
    with pytest.raises(InvalidTransition):
        rig.transition(task, S.CANCELED, "cancel", reason="")
    with pytest.raises(InvalidTransition):
        rig.transition(task, S.DUPLICATE, "duplicate")
    task = rig.transition(task, S.CANCELED, "cancel")
    assert task.observed_status == S.CANCELED


def test_p06_live_worker_must_be_quiescent_before_hold(rig):
    task, attempt = rig.running()
    with pytest.raises(InvalidTransition):
        rig.transition(task, S.ON_HOLD, "hold")
    proof = rig.proof(
        "quiescence", task.id, {"attempt_id": attempt.id, "quiescent": True}
    )
    task = rig.transition(task, S.ON_HOLD, "hold", {"quiescence": proof})
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "paused"
    task, attempt = rig.running(task)
    assert task.observed_status == S.IN_PROGRESS


def test_delivery_proofs_bind_scope_commit_and_artifact(rig):
    task = review(rig)
    proofs = rig.delivery(task)
    evidence = rig.proofs.evidence[proofs["staging"]]
    from dataclasses import replace

    rig.proofs.evidence[proofs["staging"]] = replace(
        evidence, payload={**evidence.payload, "commit": "other-commit"}
    )
    with pytest.raises(EvidenceRequired):
        rig.transition(task, S.IN_STAGING, "review_decision", proofs)
    evidence = rig.proofs.evidence[proofs["merge"]]
    rig.proofs.evidence[proofs["merge"]] = replace(
        evidence, payload={**evidence.payload, "prose_digest": "old-scope"}
    )
    with pytest.raises(EvidenceRequired):
        rig.transition(task, S.IN_DEV, "review_decision", {"merge": proofs["merge"]})
