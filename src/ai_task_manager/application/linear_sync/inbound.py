import json
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import revision
from ai_task_manager.domains.work.errors import InvalidTransition, WorkError
from ai_task_manager.domains.work.models import Cause

from .observations import record_scope_removal


class InboundDrain:
    def __init__(
        self,
        core,
        client,
        journal,
        *,
        clock,
        authorized_actors,
        evidence_for=lambda task, event: {},
    ):
        self.core, self.client, self.journal = core, client, journal
        self.clock, self.authorized_actors, self.evidence_for = (
            clock,
            frozenset(authorized_actors),
            evidence_for,
        )

    def retry_conflict(self, handle, receipt_id):
        _, scope = self.core.session_binding(handle)
        self.journal.retry_delivery(scope, receipt_id)

    def fresh_readback_ref(self, handle, task_id, observation_id):
        _, scope = self.core.session_binding(handle)
        task = self.core.get_task(handle, task_id)
        observation = next(
            (
                item
                for item in self.core.external.observations(handle, task.id)
                if item["id"] == observation_id and item["state"] == "pending"
            ),
            None,
        )
        if observation is None:
            raise LinearError("staged_observation_not_available")
        original = json.loads(observation["payload"])
        snapshot = self.client.read_issue(task.issue_id)
        if (
            snapshot.revision != observation["source_revision"]
            or snapshot.title != original["title"]
            or snapshot.body != original["body"]
        ):
            raise LinearError("staged_readback_changed")
        return self.journal.prose_readback_receipt(scope, task, snapshot)

    def _responses(self, scope, operation):
        """Retained responses from this scoped operation's current attempt only."""
        return [
            json.loads(send["snapshot"])
            for send in self.journal.sends(scope)
            if send["operation_id"] == operation.id
            and send["attempt"] == operation.attempt_count
            and send["snapshot"] is not None
        ]

    def next_receipt(self, handle):
        """Choose eligible durable work without reading the provider.

        A checkpoint waiting for an outbound outcome is only a scheduling hint.
        A newer event for the same issue runs under its own receipt and actor;
        fresh readback and human-precedence checks still decide its outcome.
        """
        _, scope = self.core.session_binding(handle)
        rows = self.journal.deliveries(scope)
        operations = None
        now = self.clock()
        fresh = {}
        for row in rows:
            if row["state"] == "received" or (
                row["state"] == "leased" and row["lease_until"] <= now
            ):
                if not json.loads(row["checkpoint"]).get("awaiting_outbound"):
                    data = json.loads(row["payload"])["data"]
                    fresh.setdefault(data["id"], []).append(
                        (row["id"], revision(data["updatedAt"]))
                    )
        for row in rows:
            if row["state"] != "received" and not (
                row["state"] == "leased" and row["lease_until"] <= now
            ):
                continue
            waiting = json.loads(row["checkpoint"]).get("awaiting_outbound")
            if waiting:
                if operations is None:
                    operations = {o.id: o for o in self.core.sync.list_operations(handle)}
                task = self.core.get_task(handle, waiting["task_id"])
                operation = operations.get(waiting["operation_id"])
                payload = json.loads(row["payload"])
                if (
                    task.sync_state != "confirmed"
                    and operation is not None
                    and operation.task_id == task.id
                    and task.issue_id == payload["data"]["id"]
                    and not operation.fenced_by
                    and operation.state in {"pending", "leased", "uncertain"}
                    and task.desired_status == waiting["desired_status"]
                    and task.decision_revision == waiting["decision_revision"]
                ):
                    responses = self._responses(scope, operation)
                    snapshot = json.loads(row["checkpoint"]).get("snapshot")
                    if responses and snapshot not in responses:
                        return row["id"]
                    if (
                        not responses
                        and operation.state == "leased"
                        and operation.lease_until <= now
                    ):
                        return row["id"]
                    # Process a newer receipt under its own identity and actor.
                    # Rereading the parked row could repark it indefinitely when
                    # the provider readback lags behind the newer event.
                    for candidate_id, candidate_revision in fresh.get(payload["data"]["id"], []):
                        if candidate_revision > revision(payload["data"]["updatedAt"]):
                            return candidate_id
                    continue
            return row["id"]
        return None

    def run_once(self, handle, worker_id="linear-inbound", *, check_authority=None):
        # The installed owner supplies its existing local enrollment/source
        # check. It is a consumption guard, never a new authority issuer.
        if check_authority is not None:
            check_authority()
        worker_id = f"{worker_id}:{uuid4()}"
        _, scope = self.core.session_binding(handle)
        receipt_id = self.next_receipt(handle)
        if receipt_id is None:
            return None
        receipt = self.journal.claim_delivery(scope, worker_id, self.clock(), receipt_id=receipt_id)
        if receipt is None:
            return None
        authority_lost = False

        def check():
            nonlocal authority_lost
            if check_authority is not None:
                try:
                    check_authority()
                except BaseException:
                    authority_lost = True
                    raise

        def applied():
            check()
            ref = self.journal.application_receipt(scope, receipt["delivery_id"], payload)
            check()
            self.core.sync.mark_applied(handle, receipt["delivery_id"], ref)
            check()
            self.journal.finish_delivery(receipt["id"], worker_id, "applied", self.clock())

        payload = json.loads(receipt["payload"])
        checkpoint = json.loads(receipt["checkpoint"])
        task = next(
            (
                task
                for task in self.core.list_tasks(handle)
                if task.issue_id == payload["data"]["id"]
            ),
            None,
        )
        try:
            check()
            if task is None:
                self.journal.finish_delivery(
                    receipt["id"], worker_id, "ignored", self.clock(), "unregistered_issue"
                )
                return {"state": "ignored", "reason": "unregistered_issue"}
            if payload["data"].get("scopeRemoval") is True:
                check()
                record_scope_removal(
                    self.core,
                    self.journal,
                    handle,
                    task,
                    payload["data"],
                    f"linear-scope-removal:{receipt['id']}",
                    check_authority=check,
                )
                raise LinearError("registered_issue_scope_removed")
            if payload["action"] == "remove":
                ref = self.journal.removal_receipt(scope, task, payload, self.authorized_actors)
                check()
                self.core.external.observe(
                    handle,
                    task.id,
                    task.version,
                    task.decision_revision,
                    ref,
                    f"linear-removal:{receipt['id']}",
                )
                raise LinearError("remote_issue_removed")
            snapshot = self.client.read_issue(task.issue_id)
            check()
            checkpoint = self.journal.checkpoint(
                receipt["id"], worker_id, {"snapshot": asdict(snapshot)}, self.clock()
            )
            # Confirmation can commit in another process during the provider read.
            task = self.core.get_task(handle, task.id)
            matching_pending = next(
                (
                    operation
                    for operation in self.core.sync.list_operations(handle)
                    if operation.task_id == task.id
                    and not operation.fenced_by
                    and operation.state in {"pending", "leased", "uncertain"}
                    and operation.payload["desired_status"] == snapshot.status
                    and snapshot.assignee_id == scope.principal
                    and snapshot.title == task.title
                    and snapshot.body == task.body
                ),
                None,
            )
            # Status/prose alone cannot attribute a newer human revision to our
            # write. The retained response binds an exact snapshot to this scoped
            # operation and attempt, even when its confirmation was lost.
            responses = self._responses(scope, matching_pending) if matching_pending else []
            known_pending_echo = asdict(snapshot) in responses
            if revision(payload["data"]["updatedAt"]) != snapshot.revision:
                if not (
                    known_pending_echo
                    and revision(payload["data"]["updatedAt"]) < snapshot.revision
                ):
                    ref = self.journal.external_receipt(scope, task, snapshot, {}, frozenset())
                    check()
                    self.core.external.observe(
                        handle,
                        task.id,
                        task.version,
                        task.decision_revision,
                        ref,
                        f"linear-reorder-observe:{receipt['id']}:{snapshot.revision}",
                    )
                raise LinearError("event_readback_revision_conflict")
            if task.sync_state == "confirmed" and self.journal.is_echo(scope, snapshot):
                applied()
                return {"state": "applied", "classification": "outbound_echo"}
            if (
                matching_pending
                and (responses or matching_pending.state == "uncertain")
                and not known_pending_echo
            ):
                matching_pending = None
            if matching_pending and task.sync_state != "confirmed":
                if (
                    not responses
                    and matching_pending.state == "leased"
                    and matching_pending.lease_until <= self.clock()
                ):
                    # Expiry does not prove rollback or a human decision. Keep
                    # the original send unresolved without waiting for an
                    # unrelated outbound claim or reading the provider again.
                    raise LinearError("outbound_response_unknown", uncertain=True)
                check()
                self.journal.checkpoint(
                    receipt["id"],
                    worker_id,
                    {
                        "awaiting_outbound": {
                            "task_id": task.id,
                            "operation_id": matching_pending.id,
                            "desired_status": task.desired_status,
                            "decision_revision": task.decision_revision,
                        }
                    },
                    self.clock(),
                )
                self.journal.release_delivery(receipt["id"], worker_id, self.clock())
                return {"state": "pending", "reason": "awaiting_outbound_readback"}
            if "observe" not in checkpoint:
                ref = self.journal.external_receipt(
                    scope, task, snapshot, payload, self.authorized_actors
                )
                checkpoint = self.journal.checkpoint(
                    receipt["id"],
                    worker_id,
                    {
                        "observe": {
                            "task_id": task.id,
                            "expected_version": task.version,
                            "expected_decision_revision": task.decision_revision,
                            "observation_ref": ref,
                            "operation_key": f"linear-inbound:{receipt['id']}:observe",
                        }
                    },
                    self.clock(),
                )
            check()
            observed = self.core.external.observe(handle, **checkpoint["observe"])
            if observed["state"] in {"stale", "duplicate"}:
                check()
                self.journal.finish_delivery(
                    receipt["id"], worker_id, "ignored", self.clock(), observed["state"]
                )
                return {"state": "ignored", "reason": observed["state"]}
            already = next(
                (
                    item
                    for item in self.core.external.observations(handle, task.id)
                    if item["id"] == observed["observation_id"]
                ),
                None,
            )
            if already and already["state"] == "applied":
                applied()
                return {"state": "applied", "classification": "recovered_application"}
            task = self.core.get_task(handle, task.id)
            refs = self.evidence_for(task, payload)
            # Enumerate causes, but delegate every allowed pair and destination guard to the core.
            # Failed candidates roll back completely; this adapter owns no policy table.
            causes = [Cause.REVIEW_DECISION] if task.observed_status == "In Review" else list(Cause)
            settled = None
            for cause in causes:
                try:
                    check()
                    settled = self.core.external.settle(
                        handle,
                        task.id,
                        observed["observation_id"],
                        task.version,
                        task.decision_revision,
                        cause,
                        refs,
                        f"linear-inbound:{receipt['id']}:settle:{task.version}:{cause}",
                    )
                    break
                except InvalidTransition:
                    continue
            if settled is None:
                raise InvalidTransition("Remote transition has no permitted cause and evidence")
            applied()
            return {"state": "applied", "task_id": task.id, "task": settled}
        except (LinearError, WorkError) as exc:
            if authority_lost:
                raise
            check()
            category = exc.category if isinstance(exc, LinearError) else type(exc).__name__
            self.journal.finish_delivery(
                receipt["id"], worker_id, "conflict", self.clock(), category
            )
            return {"state": "conflict", "error": category, "task_id": task.id if task else None}
        finally:
            if authority_lost:
                # Preserve acceptance/checkpoints for a later valid owner. If the
                # lease already expired, its existing recovery wake still owns it.
                try:
                    self.journal.release_delivery(receipt["id"], worker_id, self.clock())
                except LinearError as exc:
                    if exc.category != "lost_receiver_lease":
                        raise
