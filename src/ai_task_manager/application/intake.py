import json
import re
from dataclasses import asdict, replace
from uuid import uuid4

from ai_task_manager.domains.work.errors import (
    ApprovalRequired,
    EvidenceRequired,
    InvalidTransition,
    ValidationError,
)
from ai_task_manager.domains.work.models import (
    DELIVERED,
    Approval,
    RenderedProse,
    SyncState,
    Task,
    valid_display_identifier,
)
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import attempts, issue_claims, outbox, sessions, work

from .context import valid_prose


def prose_digest(prose, rendered):
    return work.fingerprint(
        {
            "title": rendered.title,
            "body": rendered.body,
            "representation": rendered.representation,
            "code_bearing": prose.code_bearing,
            "required_work": list(prose.required_work),
        }
    )


def submit_payload(prose, approval_ref, parent_task_id, followup_ref):
    return {
        "command": "submit",
        "prose": asdict(prose),
        "approval_ref": approval_ref,
        "parent_task_id": parent_task_id,
        "followup_ref": followup_ref,
    }


class Intake:
    def __init__(self, context):
        self.c = context

    def approved(self, prose, approval_ref, scope=None):
        valid_prose(prose)
        rendered = self.c.renderer.render(prose)
        digest = prose_digest(prose, rendered)
        if not approval_ref:
            raise ApprovalRequired("Exact rendered prose approval is required")
        if scope is None:
            approval = self.c.approvals.verify_approval(approval_ref)
        else:
            verifier = getattr(self.c.approvals, "verify_scoped_approval", None)
            if verifier is None:
                raise ApprovalRequired("A scoped current writing approval is required")
            approval = verifier(approval_ref, scope)
        if (
            not isinstance(approval, Approval)
            or approval.digest != digest
            or approval.policy != self.c.approval_policy
            or approval.representation != rendered.representation
        ):
            raise ApprovalRequired("Prose or frozen approval references do not match")
        return rendered, digest

    @staticmethod
    def _literal_spans_only(before, after):
        # Only insert single-line code delimiters around existing file/path tokens.
        i = j = 0
        changed = False
        while i < len(before) and j < len(after):
            if before[i] == after[j]:
                i += 1
                j += 1
                continue
            match = re.match(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)`", after[j:])
            if not match or not before.startswith(match[1], i):
                return False
            i += len(match[1])
            j += len(match[0])
            changed = True
        return changed and i == len(before) and j == len(after)

    @staticmethod
    def _unsettled_preparation(conn, sid):
        unsettled = []
        for row in conn.execute(
            "SELECT * FROM outbox WHERE session_id=? AND state IN ('pending','leased','uncertain')",
            (sid,),
        ).fetchall():
            op = outbox.from_row(row)
            repaired = False
            if op.kind == "create_backlog" and op.state == "uncertain" and op.fenced_by:
                event = conn.execute(
                    "SELECT kind,task_id,payload FROM events WHERE id=? AND session_id=?",
                    (op.fenced_by, sid),
                ).fetchone()
                if (
                    event
                    and event["kind"] == "pending_intake_prose_repaired"
                    and event["task_id"] == op.task_id
                ):
                    fact = json.loads(event["payload"])
                    updates = [
                        outbox.from_row(r)
                        for r in conn.execute(
                            "SELECT * FROM outbox WHERE session_id=? AND task_id=? "
                            "AND kind='update_prose'",
                            (sid, op.task_id),
                        ).fetchall()
                    ]
                    repaired = fact.get("create_operation_id") == op.id and any(
                        update.state == "confirmed"
                        and not update.fenced_by
                        and update.payload.get("event_id") == op.fenced_by
                        and update.payload.get("prose_digest") == fact.get("prose_digest")
                        and update.payload.get("issue_id") == fact.get("issue_id")
                        for update in updates
                    )
            if not repaired:
                unsettled.append(op)
        return unsettled

    def require_settled_preparation(self, handle):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            if self._unsettled_preparation(conn, sid):
                raise EvidenceRequired(
                    "Unsettled effects require reconciliation before preparation"
                )

    def pending_create_repair_replay(
        self, handle, task_id, create_operation_id, prose, approval_ref, key
    ):
        rendered, digest = self.approved(prose, approval_ref)
        payload = dict(
            command="repair_pending_create",
            task_id=task_id,
            create_operation_id=create_operation_id,
            prose=asdict(prose),
            approval_ref=approval_ref,
        )
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            self.approved(prose, approval_ref, scope)
            prior = work.replay(conn, sid, key, payload)
            if prior is None:
                return None
            task = work.get(conn, sid, task_id)
            if (
                task.prose_digest != digest
                or task.body != rendered.body
                or task.issue_id != prior["issue_id"]
            ):
                raise EvidenceRequired("Repaired intake changed after its recorded operation")
            return task

    def repair_pending_create(
        self, handle, task_id, create_operation_id, prose, approval_ref, proof_ref, key
    ):
        rendered, digest = self.approved(prose, approval_ref)
        payload = dict(
            command="repair_pending_create",
            task_id=task_id,
            create_operation_id=create_operation_id,
            prose=asdict(prose),
            approval_ref=approval_ref,
        )

        def action(conn, sid, scope):
            self.approved(prose, approval_ref, scope)
            task = work.get(conn, sid, task_id)
            operation = outbox.get(conn, sid, create_operation_id)
            proof = self.c.proof(proof_ref, "external_prose_readback", scope, task_id).payload
            if (
                task.issue_id is not None
                or task.execution_state != "pending_intake"
                or task.observed_status is not None
                or task.desired_status != S.BACKLOG
                or task.queued_once
                or task.delivered_once
                or task.human_hold
                or task.completed_work
                or task.decision_revision != 0
                or conn.execute(
                    "SELECT 1 FROM attempts WHERE session_id=? LIMIT 1", (sid,)
                ).fetchone()
                or issue_claims.known_delivered(conn, scope, task)
            ):
                raise InvalidTransition("Repair requires a never-executed pending intake")
            if (
                operation.task_id != task.id
                or operation.kind != "create_backlog"
                or operation.state != "uncertain"
                or operation.fenced_by
                or operation.error != "readback_conflict"
                or {op.id for op in self._unsettled_preparation(conn, sid)} != {operation.id}
                or operation.payload.get("task_version") != task.version
                or operation.payload.get("decision_revision") != task.decision_revision
                or operation.payload.get("prose_digest") != task.prose_digest
            ):
                raise EvidenceRequired("The original known mismatched create is required")
            if (
                rendered.title != task.title
                or prose.code_bearing != task.code_bearing
                or tuple(prose.required_work) != task.required_work
                or not self._literal_spans_only(task.body, rendered.body)
            ):
                raise ApprovalRequired("Repair permits only newly approved file code spans")
            if (
                proof.get("repair_kind") != "pending_create_code_spans"
                or proof.get("create_operation_id") != operation.id
                or proof.get("create_attempt") != operation.attempt_count
                or proof.get("task_version") != task.version
                or proof.get("decision_revision") != task.decision_revision
                or proof.get("old_prose_digest") != task.prose_digest
                or proof.get("new_prose_digest") != digest
                or proof.get("status") != S.BACKLOG
                or proof.get("title") != task.title
                or proof.get("body") == task.body
                or proof.get("assigned_to_owner") is not True
                or not proof.get("send_id")
                or not proof.get("connection_digest")
            ):
                raise EvidenceRequired("Fresh original-send repair observation is required")
            issue_id = proof.get("issue_id")
            if not isinstance(issue_id, str) or not issue_id.strip():
                raise EvidenceRequired("Known created issue identity is required")
            if issue_claims.known_delivered(conn, scope, replace(task, issue_id=issue_id)):
                raise InvalidTransition("A historically delivered issue cannot repair intake")
            identity_key = work.fingerprint(
                dict(provider=scope.provider, workspace=scope.workspace, issue_id=issue_id)
            )
            binding = conn.execute(
                "SELECT task_id FROM issue_bindings WHERE identity_key=?", (identity_key,)
            ).fetchone()
            if binding and binding[0] != task.id:
                raise EvidenceRequired("Created issue belongs to another canonical task")
            conn.execute(
                "INSERT OR IGNORE INTO issue_bindings VALUES(?,?)", (identity_key, task.id)
            )
            old_approval = task.approval_ref
            task = work.save(
                conn,
                replace(
                    task,
                    issue_id=issue_id,
                    observed_status=S.BACKLOG,
                    body=rendered.body,
                    prose_digest=digest,
                    representation=rendered.representation,
                    approval_ref=approval_ref,
                    sync_state=SyncState.PENDING,
                ),
                task.version,
            )
            self.c.changed(
                conn,
                task,
                "pending_intake_prose_repaired",
                dict(
                    repair_key=key,
                    create_operation_id=operation.id,
                    old_approval_ref=old_approval,
                    approval_ref=approval_ref,
                    repair_readback_ref=proof_ref,
                    title=task.title,
                    body=task.body,
                    issue_id=issue_id,
                    prose_digest=digest,
                    required_work=list(task.required_work),
                ),
                "update_prose",
            )
            update = conn.execute(
                "SELECT payload FROM outbox WHERE session_id=? AND task_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (sid, task.id),
            ).fetchone()
            event_id = json.loads(update["payload"])["event_id"]
            conn.execute("UPDATE outbox SET fenced_by=? WHERE id=?", (event_id, operation.id))
            return asdict(task)

        return self.c.task_mutation(handle, key, payload, action)

    def recover_submit(
        self, handle, prose, approval_ref, operation_key, parent_task_id=None, followup_ref=None
    ):
        """Read only an exact committed submission before refreshing external authority."""
        try:
            self.approved(prose, approval_ref)
        except (ApprovalRequired, ValidationError):
            # A renderer upgrade cannot reinterpret an old approval. It can only
            # return an exact, already confirmed submission without new effects.
            with self.c.db.transaction() as conn:
                sid, scope = sessions.resolve(conn, handle)
                verifier = getattr(self.c.approvals, "verify_scoped_approval", None)
                if verifier is None:
                    raise
                approval = verifier(approval_ref, scope)
                if (
                    not isinstance(approval, Approval)
                    or approval.policy != self.c.approval_policy
                    or approval.representation
                    not in getattr(self.c.renderer, "historical_representations", ())
                ):
                    raise
                prior = work.replay(
                    conn,
                    sid,
                    operation_key,
                    submit_payload(prose, approval_ref, parent_task_id, followup_ref),
                )
                if prior is None:
                    raise
                original = RenderedProse(prior["title"], prior["body"], prior["representation"])
                current = work.get(conn, sid, prior["id"])
                if (
                    approval.representation != original.representation
                    or approval.digest != prior["prose_digest"]
                    or approval.digest != prose_digest(prose, original)
                    or current.sync_state != SyncState.CONFIRMED
                    or current.issue_id is None
                ):
                    raise
                return current
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            prior = work.replay(
                conn,
                sid,
                operation_key,
                submit_payload(prose, approval_ref, parent_task_id, followup_ref),
            )
            return work.get(conn, sid, prior["id"]) if prior is not None else None

    def submit(
        self,
        handle,
        prose,
        approval_ref,
        operation_key,
        parent_task_id=None,
        followup_ref=None,
    ):
        try:
            rendered, digest = self.approved(prose, approval_ref)
        except (ApprovalRequired, ValidationError):
            return self.recover_submit(
                handle, prose, approval_ref, operation_key, parent_task_id, followup_ref
            )
        payload = submit_payload(prose, approval_ref, parent_task_id, followup_ref)

        def action(conn, sid, scope):
            if parent_task_id:
                parent = work.get(conn, sid, parent_task_id)
                if not issue_claims.known_delivered(conn, scope, parent):
                    raise InvalidTransition("Delivered-issue follow-up must link a delivered task")
                proof = self.c.proof(followup_ref, "delivered_followup", scope, parent.id)
                if (
                    proof.payload.get("prose_digest") != digest
                    or proof.payload.get("target") != S.BACKLOG
                ):
                    raise EvidenceRequired(
                        "Follow-up authorization must match the new Backlog work"
                    )
            task = Task(
                str(uuid4()),
                sid,
                rendered.title,
                rendered.body,
                digest,
                rendered.representation,
                approval_ref,
                prose.code_bearing,
                tuple(prose.required_work),
                parent_task_id=parent_task_id,
            )
            work.insert(conn, task)
            self.c.changed(
                conn,
                task,
                "task_submitted",
                {
                    "title": task.title,
                    "body": task.body,
                    "required_work": list(task.required_work),
                    "parent_task_id": parent_task_id,
                },
                "create_backlog",
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)

    def revise(
        self,
        handle,
        task_id,
        prose,
        approval_ref,
        expected_version,
        expected_decision_revision,
        decision_ref,
        operation_key,
    ):
        rendered, digest = self.approved(prose, approval_ref)
        payload = {
            "command": "revise",
            "task_id": task_id,
            "prose": asdict(prose),
            "approval_ref": approval_ref,
            "version": expected_version,
            "revision": expected_decision_revision,
            "decision_ref": decision_ref,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            if (
                issue_claims.known_delivered(conn, scope, task)
                or task.sync_state != SyncState.CONFIRMED
                or attempts.active(conn, task.id) is not None
            ):
                raise InvalidTransition(
                    "Scope changes require a quiescent undelivered confirmed task"
                )
            self.c.decision(conn, task, scope, decision_ref, task.observed_status, human=True)
            task = work.save(
                conn,
                replace(
                    task,
                    title=rendered.title,
                    body=rendered.body,
                    prose_digest=digest,
                    representation=rendered.representation,
                    approval_ref=approval_ref,
                    code_bearing=prose.code_bearing,
                    required_work=tuple(prose.required_work),
                    completed_work=(),
                    sync_state=SyncState.PENDING,
                ),
                task.version,
            )
            self.c.changed(
                conn,
                task,
                "task_scope_revised",
                {
                    "title": task.title,
                    "body": task.body,
                    "required_work": list(task.required_work),
                    "approval_ref": approval_ref,
                },
                "update_prose",
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)

    def confirm_status(
        self,
        handle,
        task_id,
        expected_version,
        expected_decision_revision,
        proof_ref,
        operation_key,
    ):
        payload = {
            "command": "confirm_status",
            "task_id": task_id,
            "version": expected_version,
            "revision": expected_decision_revision,
            "proof_ref": proof_ref,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            proof = self.c.proof(proof_ref, "provider_status", scope, task_id)
            operation = outbox.get(conn, sid, proof.payload.get("operation_id"))
            if (
                operation.task_id != task.id
                or operation.payload["task_version"] != task.version
                or proof.payload.get("status") != task.desired_status
                or operation.payload["desired_status"] != task.desired_status
                or proof.payload.get("prose_digest") != task.prose_digest
                or proof.payload.get("title") != task.title
                or proof.payload.get("body") != task.body
                or proof.payload.get("representation") != task.representation
            ):
                raise EvidenceRequired("Provider acknowledgement does not match the pending intent")
            issue_id = proof.payload.get("issue_id")
            if (
                not isinstance(issue_id, str)
                or not issue_id.strip()
                or (task.issue_id is not None and task.issue_id != issue_id)
            ):
                raise EvidenceRequired("Provider issue identity changed or is missing")
            display_identifier = proof.payload.get("display_identifier")
            if display_identifier is not None and not valid_display_identifier(display_identifier):
                raise EvidenceRequired("Provider display identifier is malformed")
            if task.issue_id is None and (
                task.desired_status != S.BACKLOG or operation.kind != "create_backlog"
            ):
                raise InvalidTransition("Initial issue must be acknowledged in Backlog")
            identity_key = work.fingerprint(
                {
                    "provider": scope.provider,
                    "workspace": scope.workspace,
                    "issue_id": issue_id,
                }
            )
            binding = conn.execute(
                "SELECT task_id FROM issue_bindings WHERE identity_key=?",
                (identity_key,),
            ).fetchone()
            if binding and binding[0] != task.id:
                raise EvidenceRequired("Provider issue is already bound to a different task")
            conn.execute(
                "INSERT OR IGNORE INTO issue_bindings VALUES(?,?)",
                (identity_key, task.id),
            )
            if task.desired_status in DELIVERED or task.desired_status in {S.CANCELED, S.DUPLICATE}:
                issue_claims.fence(
                    conn,
                    scope,
                    issue_id,
                    delivered=task.desired_status in DELIVERED,
                    terminal=task.desired_status in {S.CANCELED, S.DUPLICATE},
                    source=proof_ref,
                )
            # Review may move a delivered issue, but its historical delivery fence remains.
            execution = task.execution_state
            if execution == "pending_intake":
                execution = "idle"
            task = work.save(
                conn,
                replace(
                    task,
                    issue_id=issue_id,
                    display_identifier=display_identifier or task.display_identifier,
                    observed_status=task.desired_status,
                    sync_state=SyncState.CONFIRMED,
                    execution_state=execution,
                    queued_once=task.queued_once
                    or (
                        task.desired_status == S.TODO
                        and (
                            not issue_claims.imported(conn, task.id)
                            or (
                                operation.kind == "set_status"
                                and operation.payload.get("cause") in {"enqueue", "review_decision"}
                            )
                        )
                    ),
                    delivered_once=task.delivered_once or task.desired_status in DELIVERED,
                ),
                task.version,
            )
            outbox.confirm(conn, sid, operation.id, proof_ref)
            self.c.changed(
                conn,
                task,
                "provider_status_confirmed",
                {"observed_status": task.observed_status},
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)
