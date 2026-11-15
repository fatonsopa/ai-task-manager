"""Compose observed provider facts with canonical admission and conflict fencing."""

from uuid import uuid4

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.existing import ExistingBlocked, digest, readback_context


class ExistingIntake:
    def __init__(self, core, handle, reader):
        self.core, self.handle, self.reader = core, handle, reader

    def find(self, query, limit=20, cursor=None):
        return self.reader.find(query, limit, cursor)

    def inspect(self, issue):
        snapshot, ref = self.reader.inspect(issue)
        assessment = self.core.existing.observed(self.handle, ref)
        proof = self.reader.receipt(ref)
        return {
            key: snapshot[key]
            for key in ("issue_id", "identifier", "title", "body", "status", "source_revision")
        } | {
            "history": {
                "assessment": "eligible" if assessment.eligible else "blocked",
                "reasons": assessment.reasons,
                "digest": assessment.digest,
            },
            "binding": self.core.existing.binding(self.handle, snapshot["issue_id"]),
            "discovery_ref": ref,
            "expires_at": proof.expires_at,
        }

    def admit(
        self,
        discovery_ref,
        prose,
        writing_ref,
        selection_ref,
        operation_key,
        *,
        check_selection=lambda: None,
    ):
        check_selection()
        original = self.reader.receipt(discovery_ref)
        snapshot, fresh_ref = self.reader.readback(
            original.subject,
            readback_context("admission", operation_key, discovery_ref=discovery_ref),
        )
        check_selection()
        self.core.existing.observed(self.handle, fresh_ref)
        if digest(snapshot) != original.payload["discovery_digest"]:
            raise EvidenceRequired("Issue changed; inspect and select its current revision")
        return self.core.existing.admit(
            self.handle, discovery_ref, fresh_ref, prose, writing_ref, selection_ref, operation_key
        )

    def _fence(self, task, ref):
        self.core.external.observe(
            self.handle,
            task.id,
            task.version,
            task.decision_revision,
            ref,
            "existing-observation:" + str(uuid4()),
        )

    def guard(self, task, *, context=None):
        if not self.core.existing.is_imported(self.handle, task.id):
            return None
        try:
            snapshot, ref = (
                self.reader.readback(task.issue_id, context) if context is not None
                else self.reader.inspect(task.issue_id)
            )
        except AccessDenied:
            self._fence(task, self.reader.fence_receipt(task))
            raise ExistingBlocked("Task left managed scope; ownership reconciliation is required")
        self.core.existing.observed(self.handle, ref)
        if (snapshot["title"], snapshot["body"]) != (task.title, task.body) or snapshot[
            "status"
        ] != task.observed_status:
            self._fence(task, self.reader.fence_receipt(task, snapshot))
            raise ExistingBlocked("A current provider change requires guarded reconciliation")
        return ref

    def queue_readback(self, task_id, expected_version, decision_revision, operation_key):
        task = self.core.get_task(self.handle, task_id)
        self.core.context.check(task, expected_version, decision_revision)
        return self.guard(task, context=readback_context(
            "queue", operation_key, task_id=task_id, expected_version=expected_version,
            decision_revision=decision_revision,
        ))

    def worker_readbacks(self, task_id, root_operation_key):
        def source_readbacks(*, phase, task_id: str, expected_version, decision_revision,
                             attempt_id, operation_key):
            if (task_id != bound_task or phase not in {"reserve", "start"}
                    or operation_key != root_operation_key + ":" + phase):
                raise EvidenceRequired("Startup readback belongs to another task or operation")
            task = self.core.get_task(self.handle, bound_task)
            self.core.context.check(task, expected_version, decision_revision)
            return self.guard(task, context=readback_context(
                "start", operation_key, phase=phase, task_id=bound_task, attempt_id=attempt_id,
                expected_version=expected_version, decision_revision=decision_revision,
            ))
        bound_task = task_id
        return source_readbacks
