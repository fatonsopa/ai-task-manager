import json
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.adapters.linear.errors import LinearError
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

    def run_once(self, handle, worker_id="linear-inbound", *, check_authority=None):
        # The installed owner supplies its existing local enrollment/source
        # check. It is a consumption guard, never a new authority issuer.
        if check_authority is not None:
            check_authority()
        worker_id = f"{worker_id}:{uuid4()}"
        _, scope = self.core.session_binding(handle)
        receipt = self.journal.claim_delivery(scope, worker_id, self.clock())
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
            from ai_task_manager.adapters.linear.models import revision

            if revision(payload["data"]["updatedAt"]) != snapshot.revision:
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
            matching_pending = any(
                operation.task_id == task.id
                and not operation.fenced_by
                and operation.state in {"pending", "leased"}
                and operation.payload["desired_status"] == snapshot.status
                for operation in self.core.sync.list_operations(handle)
            )
            if matching_pending and task.sync_state != "confirmed":
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
