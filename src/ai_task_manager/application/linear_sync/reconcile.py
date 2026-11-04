import json
import time

from ai_task_manager.adapters.linear.errors import LinearError, ScopeMismatch
from ai_task_manager.domains.work.models import SyncState

from .echoes import pending_echo
from .inflight import request_in_flight
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
            or (
                task.human_hold
                and not self.core.sync.authorizes_hold_publication(handle, operation.id)
            )
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

    def inspect_registered(
        self,
        handle,
        *,
        check_authority=None,
        max_tasks=None,
        after_task_id=None,
        task_ids=None,
        should_yield=lambda: False,
    ):
        """Read a bounded registered slice; all effects retain the existing Core policy."""

        def check():
            if check_authority is not None:
                check_authority()

        check()
        _, scope = self.core.session_binding(handle)
        results = []
        tasks = sorted(self.core.list_tasks(handle), key=lambda task: task.id)
        if task_ids is not None:
            selected = frozenset(task_ids)
            tasks = [task for task in tasks if task.id in selected]
        if max_tasks is not None:
            if type(max_tasks) is not int or not 1 <= max_tasks <= 32:
                raise LinearError("invalid_reconciliation_limit")
            tasks = [t for t in tasks if after_task_id is None or t.id > after_task_id] + [
                t for t in tasks if after_task_id is not None and t.id <= after_task_id
            ]
            tasks = tasks[:max_tasks]
        self.client.check_connection(scope)
        for task in tasks:
            check()
            if results and should_yield():
                break
            if task.issue_id is None:
                results.append({"task_id": task.id, "state": "unconfirmed_intake"})
                continue
            try:
                snapshot = self.client.read_issue(task.issue_id)
                check()
                self.client.require_delegate(snapshot)
                current = self.core.get_task(handle, task.id)
                if (current.version, current.decision_revision) != (
                    task.version,
                    task.decision_revision,
                ):
                    results.append({"task_id": task.id, "state": "superseded"})
                    continue
            except ScopeMismatch as exc:
                check()
                if exc.observation:
                    record_scope_removal(
                        self.core,
                        self.journal,
                        handle,
                        task,
                        exc.observation,
                        f"linear-recovery-scope:{task.id}:{exc.observation['updatedAt']}:{task.version}",
                        check_authority=check,
                    )
                results.append({"task_id": task.id, "state": "scope_conflict"})
                continue
            pending, _, known_echo = pending_echo(
                self.core, self.journal, handle, scope, task, snapshot
            )
            if (
                pending
                and not known_echo
                and pending.state == "leased"
                and pending.lease_until > self.core.context.clock()
                and (
                    not task.human_hold
                    or self.core.sync.authorizes_hold_publication(handle, pending.id)
                )
                and pending.payload["task_version"] == task.version
                and pending.payload["decision_revision"] == task.decision_revision
                and request_in_flight(self.journal, scope, pending)
            ):
                # This is bounded deferral, not snapshot attribution. A returned
                # response/readback must still prove the effect. Different fields
                # fail pending matching and retain immediate observation below.
                check()
                results.append(
                    {
                        "task_id": task.id,
                        "state": "outbound_in_flight",
                        "retry_at": time.monotonic()
                        + max(0, pending.lease_until - self.core.context.clock()),
                    }
                )
                continue
            # A response can be published while the ownership probe runs. If
            # the request is no longer live, read its durable result after that
            # probe so the send cannot fall between the two classifications.
            if pending and not known_echo:
                pending, _, known_echo = pending_echo(
                    self.core, self.journal, handle, scope, task, snapshot
                )
            if (
                known_echo
                and task.sync_state != SyncState.CONFIRMED
                and pending.payload["task_version"] == task.version
                and pending.payload["decision_revision"] == task.decision_revision
            ):
                # The normal confirmation/recovery path still owns this send.
                # Inspection neither confirms it nor manufactures a human hold.
                check()
                results.append({"task_id": task.id, "state": "pending_outbound"})
                continue
            matches = (
                snapshot.status == task.observed_status
                and snapshot.title == task.title
                and snapshot.body == task.body
                and snapshot.assignee_id == scope.principal
            )
            # A human can restore the previously observed status while our
            # request is outstanding. Equal fields do not erase that newer
            # revision; the retained snapshot distinguishes it from pre-write
            # readback that still shows the original unchanged state.
            previous = self.journal.latest_snapshot(scope, task.issue_id)
            pending_revision_changed = (
                task.sync_state != SyncState.CONFIRMED
                and previous is not None
                and previous["revision"] < snapshot.revision
            )
            if not matches or pending_revision_changed:
                check()
                ref = self.journal.external_receipt(scope, task, snapshot, {}, frozenset())
                check()
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
