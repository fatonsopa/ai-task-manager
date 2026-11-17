"""Composition facade; adapters use these cases rather than changing SQL state."""

import time

from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import ApprovalPolicy
from ai_task_manager.domains.work.ports import ExactRenderer, RejectProofs
from ai_task_manager.storage import attempts, capacity, sessions, work

from .context import Context
from .decisions import Decisions
from .execution import Execution
from .existing_issues import ExistingIssues
from .external_sync import ExternalSynchronization
from .intake import Intake
from .notifications import Notifications
from .publication_fences import PublicationFences
from .scheduling import Scheduling
from .synchronization import Synchronization
from .transitions import Transitions


class Core:
    def __init__(
        self,
        db,
        approval_policy: ApprovalPolicy,
        capacity_policies=(),
        *,
        principals=None,
        approvals=None,
        evidence=None,
        renderer=None,
        clock=time.time,
        scheduling_sources=None,
        resource_sources=None,
    ):
        reject = RejectProofs()
        db.clock = clock
        self.context = Context(
            db,
            approval_policy,
            principals or reject,
            approvals or reject,
            evidence or reject,
            renderer or ExactRenderer(),
            clock,
        )
        with db.transaction() as conn:
            for policy in capacity_policies:
                capacity.configure(conn, policy)
        self.intake = Intake(self.context)
        self.transitions = Transitions(self.context)
        self.execution = Execution(self.context, resource_sources)
        self.context.execution = self.execution
        self.publications = PublicationFences(self.context, self.execution)
        self.existing = ExistingIssues(self.context)
        self.decisions = Decisions(self.context)
        self.notifications = Notifications(self.context)
        self.sync = Synchronization(self.context)
        self.external = ExternalSynchronization(self.context)
        self.scheduling = Scheduling(self.context, scheduling_sources)
        self.context.scheduling = self.scheduling

    def register_session(self, principal_ref, resume_ref=None):
        scope = self.context.principals.verify_principal(principal_ref)
        with self.context.db.transaction() as conn:
            proof = self.context.evidence.verify_evidence(resume_ref) if resume_ref else None
            if proof and proof.expires_at is not None and proof.expires_at <= self.context.clock():
                raise EvidenceRequired("Session resume proof has expired")
            return sessions.register(conn, scope, proof, resume_ref)

    def session_binding(self, handle):
        with self.context.db.transaction() as conn:
            return sessions.resolve(conn, handle)

    def successor_subject(self, old_handle, observation_ref):
        """Read retained task facts only under a fresh current observation grant."""
        import json

        from ai_task_manager.domains.work.errors import AccessDenied
        from ai_task_manager.domains.work.models import Scope
        from ai_task_manager.storage import sessions, work

        with self.context.db.transaction() as conn:
            row = conn.execute(
                "SELECT id,scope FROM sessions WHERE token_hash=?",
                (sessions.token_hash(old_handle.token),),
            ).fetchone()
            if row is None:
                raise AccessDenied("Original Core capability identity is unavailable")
            scope = Scope(**json.loads(row["scope"]))
            proof = self.context.evidence.verify_evidence(observation_ref)
            self.context.proof(
                observation_ref, "workflow_successor_observation", scope, proof.subject
            )
            return work.get(conn, row["id"], proof.subject)

    def admit_workflow_successor(self, old_handle, new_handle, authorization, publish):
        from .workflows.successor_authority import admit

        return admit(self, old_handle, new_handle, authorization, publish)

    def retirement_subject(self, old_handle, observation_ref, attempt_id):
        """Read one retained task and attempt; never restore execution authority."""
        import json

        from ai_task_manager.domains.work.errors import AccessDenied
        from ai_task_manager.domains.work.models import Scope

        with self.context.db.transaction() as conn:
            row = conn.execute(
                "SELECT id,scope FROM sessions WHERE token_hash=?",
                (sessions.token_hash(old_handle.token),),
            ).fetchone()
            allowed = None
            if row is None:
                row = conn.execute(
                    "SELECT s.id,s.scope,c.task_ids FROM task_capabilities c "
                    "JOIN sessions s ON s.id=c.session_id WHERE c.token_hash=?",
                    (sessions.token_hash(old_handle.token),),
                ).fetchone()
                if row is not None:
                    allowed = json.loads(row["task_ids"])
            if row is None:
                raise AccessDenied("Original Core retirement identity is unavailable")
            scope = Scope(**json.loads(row["scope"]))
            proof = self.context.evidence.verify_evidence(observation_ref)
            self.context.proof(
                observation_ref, "workflow_retirement_observation", scope, proof.subject
            )
            if allowed is not None and proof.subject not in allowed:
                raise AccessDenied("Retirement observation belongs to another task family")
            task = work.get(conn, row["id"], proof.subject)
            attempt = attempts.get(conn, row["id"], attempt_id)
            if attempt.task_id != task.id:
                raise AccessDenied("Retirement attempt belongs to another task")
            return task, attempt

    def revoke_session(self, handle):
        with self.context.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            if isinstance(sid, sessions.TaskSession):
                conn.execute(
                    "UPDATE task_capabilities SET revoked=1 WHERE reference=?", (sid.reference,)
                )
            else:
                conn.execute("UPDATE sessions SET revoked=1 WHERE id=?", (sid,))

    def get_task(self, handle, task_id, *, connection=None):
        if connection is None:
            with self.context.db.transaction() as conn:
                return self.get_task(handle, task_id, connection=conn)
        self.context.db.require_transaction(connection)
        sid, _ = sessions.resolve(connection, handle)
        return work.get(connection, sid, task_id)

    def list_tasks(self, handle):
        with self.context.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return [
                work.get(conn, sid, row[0])
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE session_id=?", (sid,)
                ).fetchall()
                if sessions.permits(sid, row[0])
            ]

    def get_attempt(self, handle, attempt_id):
        with self.context.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return attempts.get(conn, sid, attempt_id)
