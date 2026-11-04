from collections.abc import Callable
from dataclasses import asdict

from ai_task_manager.domains.work.errors import (
    EvidenceRequired,
    StaleDecision,
    StaleVersion,
    ValidationError,
)
from ai_task_manager.domains.work.models import Evidence, Task, TaskProse
from ai_task_manager.storage import events, outbox, sessions, work


class Context:
    def __init__(self, db, approval_policy, principals, approvals, evidence, renderer, clock):
        self.db, self.approval_policy = db, approval_policy
        self.principals, self.approvals, self.evidence = principals, approvals, evidence
        self.renderer, self.clock = renderer, clock
        self.scheduling = None

    def proof(self, ref, kind, scope, subject) -> Evidence:
        if not ref:
            raise EvidenceRequired(f"{kind} evidence is required")
        result = self.evidence.verify_evidence(ref)
        if (
            not isinstance(result, Evidence)
            or result.kind != kind
            or result.scope != scope
            or result.subject != subject
            or (result.expires_at is not None and result.expires_at <= self.clock())
        ):
            raise EvidenceRequired(f"Evidence does not match {kind}, scope and subject")
        return result

    def check(self, task: Task, expected_version: int, decision_revision: int):
        if task.version != expected_version:
            raise StaleVersion("Task version has changed")
        if task.decision_revision != decision_revision:
            raise StaleDecision("A newer human decision takes precedence")

    def decision(self, conn, task, scope, ref, target, human=False):
        proof = self.proof(ref, "decision", scope, task.id)
        if (
            proof.payload.get("target") != target
            or proof.payload.get("revision") != task.decision_revision
        ):
            raise StaleDecision("Decision does not authorize this destination and revision")
        if human and proof.payload.get("actor") != "human":
            raise EvidenceRequired("This transition needs explicit human acceptance")
        retaining_backlog_hold = target == "Backlog" and proof.payload.get("actor") == "human"
        if task.human_hold and target != "On Hold" and not retaining_backlog_hold:
            raise StaleDecision("Human hold requires a newer explicit human decision")
        return proof

    def mutation(self, handle, key: str, payload: dict, action: Callable) -> dict:
        with self.db.transaction() as conn:
            session_id, scope = sessions.resolve(conn, handle)
            prior = work.replay(conn, session_id, key, payload)
            if prior is not None:
                return prior
            result = action(conn, session_id, scope)
            work.remember(conn, session_id, key, payload, result)
            return result

    def changed(self, conn, task, kind, payload=None, outbound_kind=None):
        payload = {
            "task_version": task.version,
            "decision_revision": task.decision_revision,
            **(payload or {}),
        }
        event = events.append(conn, task.session_id, task.id, kind, payload)
        if self.scheduling is not None:
            self.scheduling.source_changed(conn, task, event)
        if outbound_kind:
            outbox.append(
                conn,
                task.session_id,
                task.id,
                outbound_kind,
                {
                    **payload,
                    "desired_status": task.desired_status,
                    "issue_id": task.issue_id,
                    "prose_digest": task.prose_digest,
                    "event_id": event.id,
                },
            )

    def task_mutation(self, handle, key, payload, action) -> Task:
        return work.task_from(self.mutation(handle, key, payload, action))

    @staticmethod
    def task_result(task):
        return asdict(task)


def valid_prose(prose):
    if (
        not isinstance(prose, TaskProse)
        or not isinstance(prose.title, str)
        or not isinstance(prose.body, str)
        or not prose.title.strip()
        or not prose.body.strip()
        or type(prose.code_bearing) is not bool
        or not prose.required_work
        or any(not isinstance(x, str) or not x.strip() for x in prose.required_work)
        or len(set(prose.required_work)) != len(prose.required_work)
    ):
        raise ValidationError("Prose needs a title, body and distinct declared work units")
