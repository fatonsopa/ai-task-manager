import json

from ai_task_manager.adapters.linear.errors import LinearError, ScopeMismatch
from ai_task_manager.domains.work.models import SyncState

from .observations import record_scope_removal


class Reconciliation:
    """Opt-in recovery of registered identities; no discovery or queue admission."""

    def __init__(self, core, client, journal):
        self.core, self.client, self.journal = core, client, journal

    def retry_unsent(self, handle, operation_id):
        _, scope = self.core.session_binding(handle)
        operation = next(
            (item for item in self.core.sync.list_operations(handle) if item.id == operation_id),
            None,
        )
        if operation is None or operation.fenced_by:
            raise LinearError("operation_not_recoverable")
        task = self.core.get_task(handle, operation.task_id)
        if (
            task.version != operation.payload["task_version"]
            or task.human_hold
            or task.decision_revision != operation.payload["decision_revision"]
        ):
            raise LinearError("stale_recovery_authority")
        ref = self.journal.unsent_receipt(scope, operation)
        self.core.sync.reconcile_retry(handle, operation.id, ref)

    def recover_outbound(self, handle, operation_id):
        _, scope = self.core.session_binding(handle)
        operation = next(
            (x for x in self.core.sync.list_operations(handle) if x.id == operation_id), None
        )
        if operation is None or operation.fenced_by:
            raise LinearError("operation_not_recoverable")
        task = self.core.get_task(handle, operation.task_id)
        sends = [
            x
            for x in self.journal.sends(scope)
            if x["operation_id"] == operation.id and (x["snapshot"] or x["candidate_id"])
        ]
        if not sends:
            # Client-selected UUID support is disabled. A lost create response cannot be guessed.
            raise LinearError("creation_identity_uncertain", uncertain=True)
        known_id = (
            json.loads(sends[-1]["snapshot"])["id"]
            if sends[-1]["snapshot"]
            else sends[-1]["candidate_id"]
        )
        connection = self.client.check_connection(scope, refresh=True)
        self.journal.bind_connection(scope, connection)
        if any(row["connection_digest"] != connection.digest for row in sends):
            raise LinearError("agent_connection_changed", uncertain=True)
        try:
            readback = self.client.require_delegate(self.client.read_issue(known_id))
        except LinearError as exc:
            raise LinearError("creation_identity_uncertain", uncertain=True) from exc
        if (
            task.version != operation.payload["task_version"]
            or task.human_hold
            or task.decision_revision != operation.payload["decision_revision"]
        ):
            raise LinearError("stale_recovery_authority", uncertain=True)
        ref = self.journal.status_receipt(scope, task, operation, readback, connection=connection)
        confirmed = self.core.intake.confirm_status(
            handle,
            task.id,
            task.version,
            task.decision_revision,
            ref,
            f"linear-recover:{operation.id}:{operation.attempt_count}",
        )
        self.journal.finish_send(sends[-1]["id"], "confirmed", readback)
        return confirmed

    def inspect_registered(self, handle):
        _, scope = self.core.session_binding(handle)
        results = []
        for task in self.core.list_tasks(handle):
            if task.issue_id is None:
                results.append({"task_id": task.id, "state": "unconfirmed_intake"})
                continue
            try:
                snapshot = self.client.read_issue(task.issue_id)
            except ScopeMismatch as exc:
                if exc.observation:
                    record_scope_removal(
                        self.core,
                        self.journal,
                        handle,
                        task,
                        exc.observation,
                        f"linear-recovery-scope:{task.id}:{exc.observation['updatedAt']}:{task.version}",
                    )
                results.append({"task_id": task.id, "state": "scope_conflict"})
                continue
            matches = (
                snapshot.status == task.observed_status
                and snapshot.title == task.title
                and snapshot.body == task.body
                and snapshot.assignee_id == scope.principal
            )
            if not matches:
                ref = self.journal.external_receipt(scope, task, snapshot, {}, frozenset())
                self.core.external.observe(
                    handle,
                    task.id,
                    task.version,
                    task.decision_revision,
                    ref,
                    f"linear-recovery-observe:{task.id}:{snapshot.revision}:{task.version}",
                )
            results.append(
                {
                    "task_id": task.id,
                    "state": "matching"
                    if matches and task.sync_state == SyncState.CONFIRMED
                    else "conflict",
                }
            )
        return results
