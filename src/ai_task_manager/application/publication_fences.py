"""Irreversible publication fences are independent of task-status authorization."""

from dataclasses import asdict, dataclass
from uuid import uuid4

from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.storage import issue_claims, merging_fences, work


@dataclass(frozen=True)
class PublicationContext:
    reference: str
    binding_digest: str
    commit: str


class PublicationFences:
    def __init__(self, context, execution):
        self.c, self.execution = context, execution

    def begin(
        self,
        handle,
        task_id,
        attempt_id,
        expected_task_version,
        expected_attempt_version,
        decision_revision,
        decision_ref,
        prepared_intent_ref,
        operation_key,
    ):
        payload = dict(
            command="begin_publication",
            task_id=task_id,
            attempt_id=attempt_id,
            task_version=expected_task_version,
            attempt_version=expected_attempt_version,
            revision=decision_revision,
            decision_ref=decision_ref,
            intent_ref=prepared_intent_ref,
        )

        def action(conn, sid, scope):
            task, attempt, _ = self.execution._merge_snapshot(
                conn,
                handle,
                task_id,
                attempt_id,
                expected_task_version,
                expected_attempt_version,
                decision_revision,
                decision_ref,
            )
            proof = self.c.proof(prepared_intent_ref, "publication_intent", scope, task_id)
            intent = proof.payload
            expected = {
                "task_id": task.id,
                "attempt_id": attempt.id,
                "issue_id": task.issue_id,
                "prose_digest": task.prose_digest,
                "task_version": task.version,
                "attempt_version": attempt.version,
                "decision_revision": task.decision_revision,
                "decision_ref": decision_ref,
            }
            if any(intent.get(k) != v for k, v in expected.items()) or not all(
                intent.get(k)
                for k in (
                    "operation_ref",
                    "binding_digest",
                    "commit",
                    "source_ref",
                    "source_head",
                    "target_ref",
                    "old",
                    "candidate_ref",
                    "checks_ref",
                )
            ):
                raise EvidenceRequired(
                    "Prepared publication intent does not match current authority"
                )
            context = PublicationContext(
                "publication-fence:" + str(uuid4()), intent["binding_digest"], intent["commit"]
            )
            self.c.changed(
                conn,
                task,
                "publication_fence_updated",
                {
                    "issue_key": issue_claims.identity(scope, task.issue_id),
                    "context": asdict(context),
                    "intent": intent,
                    "state": "pending",
                },
            )
            return asdict(context)

        return PublicationContext(**self.c.mutation(handle, operation_key, payload, action))

    def confirm(self, handle, task_id, context, actual_merge_ref, operation_key):
        if type(context) is not PublicationContext:
            raise EvidenceRequired("An exact Core publication context is required")
        payload = dict(
            command="confirm_publication",
            task_id=task_id,
            context=asdict(context),
            proof_ref=actual_merge_ref,
        )

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            issue_key = issue_claims.identity(scope, task.issue_id)
            rows = merging_fences.pending(conn, issue_key)
            if len(rows) != 1 or rows[0]["context"] != asdict(context):
                raise EvidenceRequired("Actual publication has no exact canonical pending intent")
            proof = self.c.proof(actual_merge_ref, "merge", scope, task.id)
            if (
                proof.payload.get("intent") != rows[0]["intent"]
                or proof.payload.get("commit") != context.commit
                or proof.payload.get("publication_binding") != context.binding_digest
                or proof.payload.get("prose_digest") != rows[0]["intent"]["prose_digest"]
            ):
                raise EvidenceRequired(
                    "Actual publication differs from its original immutable intent"
                )
            issue_claims.fence(conn, scope, task.issue_id, delivered=True, source=actual_merge_ref)
            self.c.changed(
                conn,
                task,
                "publication_fence_updated",
                dict(rows[0], state="committed", actual_merge_ref=actual_merge_ref),
            )
            return {"state": "committed", "context": asdict(context)}

        return self.c.mutation(handle, operation_key, payload, action)
