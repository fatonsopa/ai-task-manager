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
    ):
        reject = RejectProofs()
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
        self.execution = Execution(self.context)
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

    def revoke_session(self, handle):
        with self.context.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            conn.execute("UPDATE sessions SET revoked=1 WHERE id=?", (sid,))

    def get_task(self, handle, task_id):
        with self.context.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return work.get(conn, sid, task_id)

    def list_tasks(self, handle):
        with self.context.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return [
                work.get(conn, sid, row[0])
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE session_id=?", (sid,)
                ).fetchall()
            ]

    def get_attempt(self, handle, attempt_id):
        with self.context.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return attempts.get(conn, sid, attempt_id)
