"""Bind an explicitly selected existing issue without creating or queueing it."""

from dataclasses import asdict
from uuid import UUID, uuid4

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.existing import ExistingBlocked, assess, digest, readback_context
from ai_task_manager.domains.work.models import (
    Evidence,
    IssueStatus,
    SyncState,
    Task,
    valid_display_identifier,
)
from ai_task_manager.storage import issue_claims, sessions, work

from .intake import Intake


def operation_payload(discovery_ref, approval_ref, selection_ref, claim_store):
    return {
        "command": "admit_existing",
        "discovery_ref": discovery_ref,
        "approval_ref": approval_ref,
        "selection_ref": selection_ref,
        "claim_store": claim_store,
    }


class ExistingIssues:
    def __init__(self, context):
        self.c = context

    def snapshot(self, ref, scope, kind="existing_issue_snapshot"):
        proof = self.c.evidence.verify_evidence(ref)
        if not isinstance(proof, Evidence) or not isinstance(proof.payload, dict):
            raise EvidenceRequired("Typed existing issue evidence is required")
        data = proof.payload
        snapshot = data.get("snapshot")
        try:
            issue_id = snapshot["issue_id"]
            if str(UUID(issue_id)) != issue_id:
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise EvidenceRequired("Canonical existing issue evidence is required") from exc
        if kind is None:
            kind = proof.kind
        if kind not in {"existing_issue_snapshot", "existing_issue_readback"}:
            raise EvidenceRequired("Existing issue observation kind is invalid")
        self.c.proof(ref, kind, scope, issue_id)
        claim_store = getattr(self.c.db, "claim_context", None)
        if (
            not claim_store
            or data.get("claim_store") != claim_store
            or data.get("discovery_digest") != digest(snapshot)
        ):
            raise EvidenceRequired("Discovery belongs to another claim store or source")
        return snapshot, assess(snapshot)

    def readback(self, ref, scope, expected_context):
        snapshot, assessment = self.snapshot(ref, scope, "existing_issue_readback")
        proof = self.c.proof(ref, "existing_issue_readback", scope, snapshot["issue_id"])
        context = proof.payload.get("context")
        try:
            if context != readback_context(**context) or context != expected_context:
                raise EvidenceRequired("Readback does not match this exact operation context")
        except (TypeError, ValueError) as exc:
            raise EvidenceRequired("Readback operation context is invalid") from exc
        return snapshot, assessment

    def observed(self, handle, ref):
        with self.c.db.transaction() as conn:
            _, scope = sessions.resolve(conn, handle)
            snapshot, assessment = self.snapshot(ref, scope, kind=None)
            if assessment.delivered or assessment.terminal:
                issue_claims.fence(
                    conn,
                    scope,
                    snapshot["issue_id"],
                    delivered=assessment.delivered,
                    terminal=assessment.terminal,
                    source=ref,
                )
            return assessment

    def binding(self, handle, issue_id):
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            row = conn.execute(
                "SELECT tasks.id,tasks.session_id FROM issue_bindings JOIN tasks "
                "ON tasks.id=issue_bindings.task_id WHERE identity_key=?",
                (issue_claims.identity(scope, issue_id),),
            ).fetchone()
            if row is None:
                return {"state": "unbound"}
            if row["session_id"] == sid:
                return {"state": "owned", "task_id": row["id"]}
            return {"state": "unavailable"}

    def recover(self, handle, discovery_ref, approval_ref, selection_ref, operation_key):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            prior = work.replay(
                conn,
                sid,
                operation_key,
                operation_payload(
                    discovery_ref,
                    approval_ref,
                    selection_ref,
                    getattr(self.c.db, "claim_context", None),
                ),
            )
            return work.get(conn, sid, prior["id"]) if prior else None

    def admit(
        self, handle, discovery_ref, fresh_ref, prose, approval_ref, selection_ref, operation_key
    ):
        payload = operation_payload(
            discovery_ref, approval_ref, selection_ref, getattr(self.c.db, "claim_context", None)
        )

        def action(conn, sid, scope):
            original, _ = self.snapshot(discovery_ref, scope)
            snapshot, assessment = self.readback(fresh_ref, scope, readback_context(
                "admission", operation_key, discovery_ref=discovery_ref
            ))
            if digest(original) != digest(snapshot):
                raise EvidenceRequired("Issue changed after inspection")
            selected = self.c.proof(
                selection_ref, "existing_issue_selection", scope, snapshot["issue_id"]
            )
            if (
                selected.payload.get("discovery_digest") != digest(snapshot)
                or selected.payload.get("claim_store") != self.c.db.claim_context
            ):
                raise EvidenceRequired("Selection does not match the inspected issue")
            rendered, prose_hash = Intake(self.c).approved(prose, approval_ref, scope)
            if (rendered.title, rendered.body) != (snapshot["title"], snapshot["body"]):
                raise EvidenceRequired("Reviewed prose must match the exact existing issue")
            if not valid_display_identifier(snapshot.get("identifier")):
                raise EvidenceRequired("The actual provider identifier is required")
            key = issue_claims.identity(scope, snapshot["issue_id"])
            row = conn.execute(
                "SELECT task_id FROM issue_bindings WHERE identity_key=?", (key,)
            ).fetchone()
            if row:
                try:
                    current = work.get(conn, sid, row["task_id"])
                except AccessDenied as exc:
                    raise AccessDenied("Issue ownership requires reconciliation") from exc
                if (
                    current.prose_digest != prose_hash
                    or current.observed_status != snapshot["status"]
                ):
                    raise ExistingBlocked(
                        "Existing binding requires its guarded reconciliation path"
                    )
                return asdict(current)
            issue_claims.require_open(conn, scope, snapshot["issue_id"])
            if not assessment.eligible:
                raise ExistingBlocked("Existing issue history or ownership is not eligible")
            task = Task(
                str(uuid4()),
                sid,
                rendered.title,
                rendered.body,
                prose_hash,
                rendered.representation,
                approval_ref,
                prose.code_bearing,
                tuple(prose.required_work),
                issue_id=snapshot["issue_id"],
                observed_status=IssueStatus(snapshot["status"]),
                desired_status=IssueStatus(snapshot["status"]),
                sync_state=SyncState.CONFIRMED,
                execution_state="idle",
                display_identifier=snapshot["identifier"],
            )
            work.insert(conn, task)
            conn.execute("INSERT INTO issue_bindings VALUES(?,?)", (key, task.id))
            conn.execute(
                "INSERT INTO existing_admissions VALUES(?,?,?)",
                (
                    task.id,
                    snapshot["source_revision"],
                    assessment.digest,
                ),
            )
            self.c.changed(
                conn,
                task,
                "existing_issue_bound",
                {
                    "source_revision": snapshot["source_revision"],
                    "history_digest": assessment.digest,
                    "discovery_ref": discovery_ref,
                    "selection_ref": selection_ref,
                    "claim_store": self.c.db.claim_context,
                    "queue_admitted": False,
                },
            )
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)

    def is_imported(self, handle, task_id):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            work.get(conn, sid, task_id)
            return issue_claims.imported(conn, task_id) is not None

    def validate_source(self, conn, task, scope, source_ref, expected_context):
        snapshot, assessment = self.readback(source_ref, scope, expected_context)
        if (
            snapshot["issue_id"] != task.issue_id
            or (snapshot["title"], snapshot["body"]) != (task.title, task.body)
            or snapshot["status"] != task.observed_status
        ):
            raise EvidenceRequired("Current imported source does not match this task")
        issue_claims.require_open(conn, scope, task.issue_id)
        allowed = (
            {
                "prior_ownership_or_hold_requires_reconciliation",
                "current_state_requires_guarded_path",
                "returned_backlog_requires_guarded_path",
            }
            if task.queued_once
            else set()
        )
        if assessment.delivered or assessment.terminal or set(assessment.reasons) - allowed:
            raise ExistingBlocked("Imported issue needs ownership/history reconciliation")
        return assessment

