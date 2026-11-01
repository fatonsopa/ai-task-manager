from dataclasses import asdict, replace

from ai_task_manager.domains.work.errors import InvalidTransition
from ai_task_manager.domains.work.models import (
    AttemptState,
    Cause,
    SyncState,
)
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.transitions import check_transition
from ai_task_manager.storage import attempts, work

from .transition_evidence import destination_facts


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
                raise InvalidTransition("Provider must confirm the previous intent first")
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
            facts, active = destination_facts(
                self.c, conn, scope, task, target, decision, evidence_refs
            )
            check_transition(task.observed_status, target, cause, facts)
            if active:
                attempts.save(
                    conn,
                    replace(active, state=AttemptState.PAUSED, version=active.version + 1),
                )
                conn.execute("DELETE FROM resource_leases WHERE attempt_id=?", (active.id,))
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
