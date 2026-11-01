"""Trusted in-process proof fixtures, never a product approval implementation."""

from ai_task_manager.application.core import Core
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    ApprovalRequired,
    EvidenceRequired,
)
from ai_task_manager.domains.work.models import (
    Approval,
    ApprovalPolicy,
    CapacityPolicy,
    Evidence,
    Scope,
    TaskProse,
)
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.ports import ExactRenderer
from ai_task_manager.storage.database import Database


class ProofFixture:
    def __init__(self):
        self.principals, self.approvals, self.evidence = {}, {}, {}

    def verify_principal(self, ref):
        if ref not in self.principals:
            raise AccessDenied("Unknown principal proof")
        return self.principals[ref]

    def verify_approval(self, ref):
        if ref not in self.approvals:
            raise ApprovalRequired("Unknown approval proof")
        return self.approvals[ref]

    def verify_evidence(self, ref):
        if ref not in self.evidence:
            raise EvidenceRequired("Unknown evidence proof")
        return self.evidence[ref]


class Rig:
    def __init__(self, path, host_limit=2, project_limit=2, renderer=None):
        self.db = Database(path)
        self.proofs = ProofFixture()
        self.policy = ApprovalPolicy(
            "writer-frozen", "issue-reference-frozen", "style-frozen"
        )
        self.now = 1000.0
        self.serial = 0
        self.renderer = renderer or ExactRenderer()
        self.capacity = CapacityPolicy("host", host_limit, project_limit)
        self.core = self.new_core()
        self.scope = Scope("linear", "workspace", "user-a", "project", "session-a")
        self.handle = self.session(self.scope)

    def new_core(self):
        return Core(
            self.db,
            self.policy,
            (self.capacity,),
            principals=self.proofs,
            approvals=self.proofs,
            evidence=self.proofs,
            renderer=self.renderer,
            clock=lambda: self.now,
        )

    def key(self):
        self.serial += 1
        return f"operation-{self.serial}"

    def session(self, scope):
        ref = self.key()
        self.proofs.principals[ref] = scope
        return self.core.register_session(ref)

    def proof(self, kind, subject, payload=None, scope=None, expires_at=None):
        ref = self.key()
        self.proofs.evidence[ref] = Evidence(
            kind, scope or self.scope, subject, payload or {}, expires_at
        )
        return ref

    def approval(self, prose):
        ref = self.key()
        rendered = self.renderer.render(prose)
        self.proofs.approvals[ref] = Approval(
            prose_digest(prose, rendered), self.policy, rendered.representation
        )
        return ref

    def task(self, *, code=True, units=("complete",), confirmed=True, handle=None):
        prose = TaskProse(
            "Verify session isolation",
            "Run `check --scope=session` and retain its result.",
            code,
            units,
        )
        handle = handle or self.handle
        task = self.core.intake.submit(handle, prose, self.approval(prose), self.key())
        return self.confirm(task, handle=handle) if confirmed else task

    def current(self, task):
        return self.core.get_task(self.handle, task.id)

    def confirm(self, task, handle=None, scope=None):
        handle = handle or self.handle
        operation = [
            x
            for x in self.core.sync.list_operations(handle)
            if x.task_id == task.id and x.state != "confirmed"
        ][-1]
        proof = self.proof(
            "provider_status",
            task.id,
            {
                "operation_id": operation.id,
                "status": task.desired_status,
                "prose_digest": task.prose_digest,
                "title": task.title,
                "body": task.body,
                "representation": task.representation,
                "issue_id": task.issue_id or f"TST:{task.id}",
            },
            scope=scope,
        )
        return self.core.intake.confirm_status(
            handle, task.id, task.version, task.decision_revision, proof, self.key()
        )

    def decision(
        self, task, target, *, actor="human", reason="Verified decision", scope=None
    ):
        return self.proof(
            "decision",
            task.id,
            {
                "target": target,
                "revision": task.decision_revision,
                "actor": actor,
                "reason": reason,
            },
            scope=scope,
        )

    def transition(
        self, task, target, cause, evidence=None, confirm=True, **decision_kwargs
    ):
        result = self.core.transitions.transition(
            self.handle,
            task.id,
            target,
            cause,
            task.version,
            task.decision_revision,
            self.decision(task, target, **decision_kwargs),
            evidence or {},
            self.key(),
        )
        return self.confirm(result) if confirm and result != task else result

    def queued(self, **kwargs):
        return self.transition(self.task(**kwargs), S.TODO, "enqueue")

    def reserved(self, task=None):
        task = task or self.queued()
        attempt = self.core.execution.reserve(
            self.handle,
            task.id,
            "host",
            task.version,
            task.decision_revision,
            self.decision(task, S.IN_PROGRESS),
            self.key(),
        )
        return self.current(task), attempt

    def starting(self, task=None):
        task, attempt = self.reserved(task)
        ref = self.proof(
            "worker_start",
            attempt.id,
            {"task_id": task.id, "host": "host", "worker_id": f"worker:{attempt.id}"},
        )
        attempt = self.core.execution.start(
            self.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            ref,
            self.decision(task, S.IN_PROGRESS),
            self.key(),
        )
        return self.current(task), attempt

    def resources(self, task, attempt, **overrides):
        payload = {
            "task_id": task.id,
            "host": attempt.host,
            "worker_id": attempt.worker_id,
            "ready": True,
            "worktree_id": f"tree:{attempt.id}",
            "database_id": f"db:{attempt.id}",
            **overrides,
        }
        return self.proof("resources_ready", attempt.id, payload)

    def running(self, task=None):
        task, attempt = self.starting(task)
        task = self.confirm(task)
        attempt = self.core.execution.running(
            self.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            self.resources(task, attempt),
            self.key(),
        )
        return self.current(task), attempt

    def settle(self, task, attempt, state="done", **overrides):
        ref = self.proof(
            "attempt_outcome",
            attempt.id,
            {
                "task_id": task.id,
                "host": attempt.host,
                "worker_id": attempt.worker_id,
                "state": state,
                "quiescent": True,
                **overrides,
            },
        )
        attempt = self.core.execution.settle(
            self.handle,
            attempt.id,
            task.version,
            task.decision_revision,
            ref,
            self.key(),
        )
        return self.current(task), attempt

    def complete(self, task, unit="complete"):
        ref = self.proof(
            "work_unit_complete",
            task.id,
            {"unit": unit, "prose_digest": task.prose_digest},
        )
        return self.core.execution.complete_unit(
            self.handle,
            task.id,
            unit,
            task.version,
            task.decision_revision,
            ref,
            self.key(),
        )

    def delivery(self, task):
        return {
            kind: self.proof(
                kind,
                task.id,
                {
                    "prose_digest": task.prose_digest,
                    "commit": "commit-a",
                    "artifact": "artifact-a",
                },
            )
            for kind in ("merge", "staging", "production", "non_code_completion")
        }
