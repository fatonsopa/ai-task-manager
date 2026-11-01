from dataclasses import asdict, replace

from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition
from ai_task_manager.domains.work.models import (
    AttemptState,
    Cause,
    SyncState,
    TransitionFacts,
)
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.transitions import check_transition
from ai_task_manager.storage import attempts, work


class Transitions:
    def __init__(self, context):
        self.c = context

    def transition(
        self,
        handle,
        task_id,
        target,
        cause,
        expected_version,
        expected_decision_revision,
        decision_ref,
        evidence_refs,
        operation_key,
    ):
        try:
            target, cause = S(target), Cause(cause)
        except ValueError as exc:
            raise InvalidTransition("Unknown status or cause") from exc
        evidence_refs = dict(evidence_refs)
        payload = {
            "command": "transition",
            "task_id": task_id,
            "target": target,
            "cause": cause,
            "version": expected_version,
            "revision": expected_decision_revision,
            "decision_ref": decision_ref,
            "evidence_refs": evidence_refs,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            if task.issue_id is None or task.sync_state != SyncState.CONFIRMED:
                raise InvalidTransition(
                    "Provider must confirm the previous intent first"
                )
            decision = self.c.decision(
                conn,
                task,
                scope,
                decision_ref,
                target,
                human=task.observed_status == S.IN_REVIEW or target == S.IN_REVIEW,
            )
            if target == task.observed_status:
                return asdict(task)
            if task.delivered_once and target == S.IN_PROGRESS:
                raise InvalidTransition("Further work needs a new linked Backlog issue")
            # Initial worker admission is deliberately available only through execution use cases.
            if target == S.IN_PROGRESS:
                raise InvalidTransition(
                    "Use guarded reserve/start; a status change cannot grant work"
                )
            active = attempts.active(conn, task.id)
            proofs = {
                kind: self.c.proof(ref, kind, scope, task.id)
                for kind, ref in evidence_refs.items()
            }
            for kind in {
                "merge",
                "staging",
                "production",
                "non_code_completion",
            } & proofs.keys():
                if proofs[kind].payload.get("prose_digest") != task.prose_digest:
                    raise EvidenceRequired(
                        "Delivery evidence must match the approved work scope"
                    )
            if "merge" in proofs and not proofs["merge"].payload.get("commit"):
                raise EvidenceRequired(
                    "Merge evidence must identify the delivered commit"
                )
            if "staging" in proofs and (
                not proofs["staging"].payload.get("artifact")
                or "merge" not in proofs
                or proofs["staging"].payload.get("commit")
                != proofs["merge"].payload["commit"]
            ):
                raise EvidenceRequired("Staging evidence must match the merged commit")
            if "production" in proofs and (
                "staging" not in proofs
                or proofs["production"].payload.get("artifact")
                != proofs["staging"].payload["artifact"]
            ):
                raise EvidenceRequired(
                    "Production evidence must match the staging artifact"
                )
            quiescent = active is None
            if active is not None and "quiescence" in proofs:
                if (
                    proofs["quiescence"].payload.get("attempt_id") != active.id
                    or proofs["quiescence"].payload.get("quiescent") is not True
                ):
                    raise EvidenceRequired(
                        "Quiescence proof must identify the active attempt"
                    )
                quiescent = True
            complete = set(task.required_work) <= set(task.completed_work)
            facts = TransitionFacts(
                authorized=True,
                queued=task.queued_once,
                quiescent=quiescent,
                reason=bool(decision.payload.get("reason", "").strip()),
                review_requested=target == S.IN_REVIEW
                and decision.payload.get("actor") == "human",
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
            check_transition(task.observed_status, target, cause, facts)
            if active:
                attempts.save(
                    conn,
                    replace(
                        active, state=AttemptState.PAUSED, version=active.version + 1
                    ),
                )
                conn.execute(
                    "DELETE FROM resource_leases WHERE attempt_id=?", (active.id,)
                )
            task = work.save(
                conn,
                replace(
                    task,
                    desired_status=target,
                    sync_state=SyncState.PENDING,
                    execution_state="idle",
                ),
                task.version,
            )
            self.c.changed(
                conn,
                task,
                "transition_requested",
                {
                    "from": task.observed_status,
                    "cause": cause,
                    "evidence_refs": evidence_refs,
                    "decision_ref": decision_ref,
                },
                "set_status",
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)
