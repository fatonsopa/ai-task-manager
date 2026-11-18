from contextlib import ExitStack
from dataclasses import asdict

from ai_task_manager.adapters.linear.errors import LinearError, ScopeMismatch
from ai_task_manager.domains.work.errors import WorkError
from ai_task_manager.domains.work.models import SyncState

from .inflight import owned_request
from .observations import ownership_removal, record_scope_removal


class OutboundDrain:
    def __init__(
        self,
        core,
        client,
        journal,
        *,
        adapter_id="linear-outbound",
        wake_inbound=lambda: None,
        check_authority=lambda: None,
    ):
        self.core, self.client, self.journal, self.adapter_id = core, client, journal, adapter_id
        self.wake_inbound = wake_inbound
        self.check_authority = check_authority

    def _wake(self, operation_id):
        try:
            self.wake_inbound()
        except (OSError, RuntimeError) as exc:
            self.journal.record_wake_failure(operation_id, type(exc).__name__)

    def _observe_conflict(self, handle, task, snapshot):
        _, scope = self.core.session_binding(handle)
        connection = self.client.check_connection(scope)
        if (
            snapshot.assignee_id != scope.principal
            or snapshot.delegate_id != connection.agent_id
            or snapshot.delegate_is_application is not True
        ):
            return record_scope_removal(
                self.core,
                self.journal,
                handle,
                task,
                ownership_removal(snapshot),
                f"linear-preflight-scope:{task.id}:{snapshot.revision}:{task.version}",
            )
        ref = self.journal.external_receipt(scope, task, snapshot, {}, frozenset())
        return self.core.external.observe(
            handle,
            task.id,
            task.version,
            task.decision_revision,
            ref,
            f"linear-preflight:{task.id}:{snapshot.revision}:{task.version}",
        )

    def _native_type(self, scope, task):
        record = getattr(self.core.context.approvals, "record", None)
        if record is None:
            return None  # Historical adapters have no new signed metadata marker.
        approved = record(task.approval_ref)
        if "native_task_type" not in approved:
            return None
        if (
            approved.get("purpose") != "task"
            or approved.get("scope") != asdict(scope)
            or approved.get("digest") != task.prose_digest
        ):
            raise LinearError("native_type_approval_scope_changed")
        return self.client.native_type(approved["native_task_type"])

    def run_once(self, handle, *, task_id=None, check_authority=None):
        check_authority = check_authority or self.check_authority
        check_authority()
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
                or (
                    task.human_hold
                    and not self.core.sync.authorizes_hold_publication(handle, operation.id)
                )
            ):
                raise LinearError("stale_outbound_authority")
            if self.client.binding is None:
                self.client.verify_binding()
            connection = self.client.check_connection(scope, refresh=True)
            self.journal.bind_connection(scope, connection)
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
                if operation.payload.get("repair_readback_ref"):
                    original = next(
                        (
                            op
                            for op in self.core.sync.list_operations(handle)
                            if op.id == operation.payload.get("create_operation_id")
                        ),
                        None,
                    )
                    repair_before = self.journal.pending_create_repair_snapshot(
                        scope, task, operation, original, connection
                    )
                    if asdict(before) != repair_before:
                        self._observe_conflict(handle, task, before)
                        raise LinearError("preflight_conflict")
                previous = self.journal.latest_snapshot(scope, task.issue_id)
                changed_revision = bool(previous and previous["revision"] != before.revision)
                if (
                    before.status != task.observed_status
                    or before.assignee_id != scope.principal
                    or (
                        before.delegate_id != connection.agent_id
                        and (
                            before.delegate_id is not None
                            or (previous and previous.get("delegate_id") is not None)
                        )
                    )
                    or (
                        operation.kind != "update_prose"
                        and (before.title != task.title or before.body != task.body)
                    )
                    or changed_revision
                ):
                    self._observe_conflict(handle, task, before)
                    raise LinearError("preflight_conflict")
            native_type = (
                self._native_type(scope, task) if operation.kind == "create_backlog" else None
            )
            # Recheck after preflight I/O; a newer local human decision fences this send.
            current = self.core.get_task(handle, task.id)
            if (
                current.version != task.version
                or current.decision_revision != task.decision_revision
                or (
                    current.human_hold
                    and not self.core.sync.authorizes_hold_publication(handle, operation.id)
                )
            ):
                raise LinearError("stale_outbound_authority")
            candidate_id = (
                operation.id
                if operation.kind == "create_backlog" and self.client.create_capability
                else None
            )
            check_authority()

            def prepare_dispatch():
                check_authority()

                def begin_dispatch():
                    nonlocal send_id
                    # Connection and credentials may perform fallible reads. Recheck
                    # the human/version fence after them, before recording any send.
                    with self.core.sync.outbound_dispatch(handle, operation):
                        send_id = self.journal.begin_send(
                            scope, operation, candidate_id,
                            connection_digest=connection.digest, native_type=native_type,
                        )
                        request.enter_context(owned_request(self.journal, scope, operation, send_id))

                return begin_dispatch

            with ExitStack() as request:
                if operation.kind == "create_backlog":
                    options = {"_prepare_dispatch": prepare_dispatch}
                    if native_type is not None:
                        options["native_type"] = native_type
                    result = (
                        self.client.create_issue(task, candidate_id, **options)
                        if candidate_id
                        else self.client.create_issue(task, **options)
                    )
                else:
                    result = self.client.update_issue(
                        task, operation.kind, _prepare_dispatch=prepare_dispatch
                    )
                if result.assignee_id != scope.principal:
                    metadata = ownership_removal(result)
                    self.journal.record_scope_response(scope, send_id, metadata)
                    if task.issue_id == result.id:
                        record_scope_removal(
                            self.core,
                            self.journal,
                            handle,
                            task,
                            metadata,
                            f"linear-response-scope:{operation.id}:{operation.attempt_count}",
                        )
                    raise LinearError("mutation_scope_changed", uncertain=True)
                self.journal.finish_send(send_id, "mutation_returned", result)
            if candidate_id is not None and result.id != candidate_id:
                raise LinearError("creation_uuid_not_honored", uncertain=True)
            self.client.check_connection(scope, refresh=True)
            readback = self.client.require_delegate(self.client.read_issue(result.id))
            if readback != result:
                self._observe_conflict(handle, self.core.get_task(handle, task.id), readback)
                raise LinearError("readback_race", uncertain=True)
            if native_type is not None:
                typed = self.client.verify_native_type(result.id, native_type)
                if typed != readback:
                    self._observe_conflict(handle, self.core.get_task(handle, task.id), typed)
                    raise LinearError("readback_race", uncertain=True)
            ref = self.journal.status_receipt(
                scope, task, operation, readback, connection=connection, native_type=native_type
            )
            self.journal.finish_send(send_id, "readback", readback)
            check_authority()
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

    def reconcile_pending(self, handle, task_id, *, check_authority=None):
        """Read retained sends without issuing another provider mutation."""
        from .reconcile import Reconciliation

        check_authority = check_authority or self.check_authority
        check_authority()
        task = self.core.get_task(handle, task_id)
        if task.sync_state == SyncState.CONFIRMED:
            return task
        operations = [
            op
            for op in self.core.sync.list_operations(handle)
            if op.task_id == task_id
            and not op.fenced_by
            and op.state in {"failed", "uncertain", "leased"}
        ]
        if len(operations) != 1:
            raise LinearError("synchronization_pending", uncertain=True)
        return Reconciliation(self.core, self.client, self.journal).recover_outbound(
            handle, operations[0].id, check_authority=check_authority
        )

    def confirm_pending(self, handle, task_id):
        task = self.core.get_task(handle, task_id)
        if task.sync_state == SyncState.CONFIRMED:
            return task
        result = self.run_once(handle, task_id=task_id)
        if result and result["state"] == "confirmed":
            return result["task"]
        if result is None:
            return self.reconcile_pending(handle, task_id)
        raise LinearError(
            result["error"] if result else "synchronization_pending",
            uncertain=not result or result["state"] == "uncertain",
        )
