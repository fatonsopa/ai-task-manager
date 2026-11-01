from ai_task_manager.adapters.linear.errors import LinearError, ScopeMismatch
from ai_task_manager.domains.work.errors import WorkError
from ai_task_manager.domains.work.models import SyncState

from .observations import record_scope_removal


class OutboundDrain:
    def __init__(
        self, core, client, journal, *, adapter_id="linear-outbound", wake_inbound=lambda: None
    ):
        self.core, self.client, self.journal, self.adapter_id = core, client, journal, adapter_id
        self.wake_inbound = wake_inbound

    def _wake(self, operation_id):
        try:
            self.wake_inbound()
        except (OSError, RuntimeError) as exc:
            self.journal.record_wake_failure(operation_id, type(exc).__name__)

    def _observe_conflict(self, handle, task, snapshot):
        _, scope = self.core.session_binding(handle)
        ref = self.journal.external_receipt(scope, task, snapshot, {}, frozenset())
        return self.core.external.observe(
            handle,
            task.id,
            task.version,
            task.decision_revision,
            ref,
            f"linear-preflight:{task.id}:{snapshot.revision}:{task.version}",
        )

    def run_once(self, handle, *, task_id=None):
        operation = self.core.sync.claim(handle, self.adapter_id, task_id=task_id)
        if operation is None:
            return None
        _, scope = self.core.session_binding(handle)
        task = self.core.get_task(handle, operation.task_id)
        send_id = None
        try:
            if (
                operation.fenced_by
                or task.version != operation.payload["task_version"]
                or task.decision_revision != operation.payload["decision_revision"]
                or task.desired_status != operation.payload["desired_status"]
                or task.human_hold
            ):
                raise LinearError("stale_outbound_authority")
            if self.client.binding is None:
                self.client.verify_binding()
            if operation.kind not in {"create_backlog", "set_status", "update_prose"}:
                raise LinearError("unsupported_outbound_kind")
            if operation.kind != "create_backlog":
                try:
                    before = self.client.read_issue(task.issue_id)
                except ScopeMismatch as exc:
                    if exc.observation:
                        record_scope_removal(
                            self.core,
                            self.journal,
                            handle,
                            task,
                            exc.observation,
                            f"linear-preflight-scope:{task.id}:{exc.observation['updatedAt']}:{task.version}",
                        )
                    raise
                previous = self.journal.latest_snapshot(scope, task.issue_id)
                changed_revision = bool(previous and previous["revision"] != before.revision)
                if (
                    before.status != task.observed_status
                    or before.assignee_id != scope.principal
                    or (
                        operation.kind != "update_prose"
                        and (before.title != task.title or before.body != task.body)
                    )
                    or changed_revision
                ):
                    self._observe_conflict(handle, task, before)
                    raise LinearError("preflight_conflict")
            # Recheck after preflight I/O; a newer local human decision fences this send.
            current = self.core.get_task(handle, task.id)
            if (
                current.version != task.version
                or current.decision_revision != task.decision_revision
                or current.human_hold
            ):
                raise LinearError("stale_outbound_authority")
            candidate_id = (
                operation.id
                if operation.kind == "create_backlog" and self.client.create_capability
                else None
            )
            send_id = self.journal.begin_send(scope, operation, candidate_id)
            result = (
                self.client.create_issue(task, candidate_id)
                if candidate_id
                else self.client.create_issue(task)
                if operation.kind == "create_backlog"
                else self.client.update_issue(task, operation.kind)
            )
            self.journal.finish_send(send_id, "mutation_returned", result)
            if candidate_id is not None and result.id != candidate_id:
                raise LinearError("creation_uuid_not_honored", uncertain=True)
            readback = self.client.read_issue(result.id)
            if readback != result:
                self._observe_conflict(handle, self.core.get_task(handle, task.id), readback)
                raise LinearError("readback_race", uncertain=True)
            ref = self.journal.status_receipt(scope, task, operation, readback)
            self.journal.finish_send(send_id, "readback", readback)
            confirmed = self.core.intake.confirm_status(
                handle,
                task.id,
                task.version,
                task.decision_revision,
                ref,
                f"linear-confirm:{operation.id}:{operation.attempt_count}",
            )
            self.journal.finish_send(send_id, "confirmed", readback)
            self._wake(operation.id)
            return {
                "operation_id": operation.id,
                "task_id": task.id,
                "state": "confirmed",
                "task": confirmed,
            }
        except (LinearError, WorkError) as exc:
            uncertain = bool(send_id) or (isinstance(exc, LinearError) and exc.uncertain)
            category = exc.category if isinstance(exc, LinearError) else type(exc).__name__
            state = "uncertain" if uncertain else "failed"
            if send_id:
                self.journal.finish_send(send_id, state, error=category)
            try:
                self.core.sync.record_outcome(
                    handle, operation.id, self.adapter_id, operation.attempt_count, state, category
                )
            except WorkError:
                # An expired lease or newer confirmed/fenced intent retains its own truth.
                pass
            self._wake(operation.id)
            return {
                "operation_id": operation.id,
                "task_id": task.id,
                "state": state,
                "error": category,
            }

    def confirm_pending(self, handle, task_id):
        task = self.core.get_task(handle, task_id)
        if task.sync_state == SyncState.CONFIRMED:
            return task
        result = self.run_once(handle, task_id=task_id)
        if result and result["state"] == "confirmed":
            return result["task"]
        raise LinearError(
            result["error"] if result else "synchronization_pending",
            uncertain=not result or result["state"] == "uncertain",
        )
