from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.models import TransitionFacts
from ai_task_manager.storage import attempts


def destination_facts(context, conn, scope, task, target, decision, evidence_refs):
    active = attempts.active(conn, task.id)
    proofs = {kind: context.proof(ref, kind, scope, task.id) for kind, ref in evidence_refs.items()}
    for kind in {
        "merge",
        "staging",
        "production",
        "non_code_completion",
    } & proofs.keys():
        if proofs[kind].payload.get("prose_digest") != task.prose_digest:
            raise EvidenceRequired("Delivery evidence must match the approved work scope")
    if "merge" in proofs and not proofs["merge"].payload.get("commit"):
        raise EvidenceRequired("Merge evidence must identify the delivered commit")
    if "staging" in proofs and (
        not proofs["staging"].payload.get("artifact")
        or "merge" not in proofs
        or proofs["staging"].payload.get("commit") != proofs["merge"].payload["commit"]
    ):
        raise EvidenceRequired("Staging evidence must match the merged commit")
    if "production" in proofs and (
        "staging" not in proofs
        or proofs["production"].payload.get("artifact") != proofs["staging"].payload["artifact"]
    ):
        raise EvidenceRequired("Production evidence must match the staging artifact")
    quiescent = active is None
    if active is not None and "quiescence" in proofs:
        if (
            proofs["quiescence"].payload.get("attempt_id") != active.id
            or proofs["quiescence"].payload.get("quiescent") is not True
        ):
            raise EvidenceRequired("Quiescence proof must identify the active attempt")
        quiescent = True
    complete = set(task.required_work) <= set(task.completed_work)
    facts = TransitionFacts(
        authorized=True,
        queued=task.queued_once,
        quiescent=quiescent,
        reason=bool(decision.payload.get("reason", "").strip()),
        review_requested=target == S.IN_REVIEW and decision.payload.get("actor") == "human",
        complete=complete,
        merged="merge" in proofs,
        staged="staging" in proofs,
        released="production" in proofs,
        non_code_verified="non_code_completion" in proofs,
        duplicate_link=bool(
            proofs.get("duplicate_target")
            and proofs["duplicate_target"].payload.get("canonical_issue_id")
            not in {None, task.issue_id}
        ),
        code_bearing=task.code_bearing,
    )
    return facts, active
