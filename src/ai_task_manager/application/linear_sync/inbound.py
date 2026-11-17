import json
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.adapters.linear.errors import LinearError, ScopeMismatch
from ai_task_manager.adapters.linear.models import revision
from ai_task_manager.domains.work.errors import InvalidTransition, WorkError
from ai_task_manager.domains.work.models import Cause

from .echoes import pending_echo, retained_responses
from .observations import ownership_removal, record_scope_removal


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
        check_authority=None,
    ):
        self.core, self.client, self.journal = core, client, journal
        self.check_authority = check_authority
        self.clock, self.authorized_actors, self.evidence_for = (
            clock,
            frozenset(authorized_actors),
            evidence_for,
        )

    def retry_conflict(self, handle, receipt_id):
        session_id, scope = self.core.session_binding(handle)
        from ai_task_manager.storage.sessions import TaskSession
        if isinstance(session_id, TaskSession):
            rows = self._scoped_deliveries(handle, self.journal.deliveries(scope))
            if receipt_id not in {row["id"] for row in rows}:
                from ai_task_manager.domains.work.errors import AccessDenied
                raise AccessDenied("Successor cannot retry another task's inbound receipt")
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

    def _scoped_deliveries(self, handle, rows):
        session_id, _ = self.core.session_binding(handle)
        from ai_task_manager.storage.sessions import TaskSession
        if not isinstance(session_id, TaskSession):
            return rows
        issues = {task.issue_id for task in self.core.list_tasks(handle)}
        return [row for row in rows if json.loads(row["payload"]).get("data", {}).get("id") in issues]

    def next_receipt(self, handle, *, task_ids=None):
        """Choose eligible durable work without reading the provider.

        A checkpoint waiting for an outbound outcome is only a scheduling hint.
        A newer event for the same issue runs under its own receipt and actor;
        fresh readback and human-precedence checks still decide its outcome.
        """
        _, scope = self.core.session_binding(handle)
        rows = self._scoped_deliveries(handle, self.journal.deliveries(scope))
        if task_ids is not None:
            selected = frozenset(task_ids)
            issues = {task.issue_id for task in self.core.list_tasks(handle) if task.id in selected}
            rows = [row for row in rows if json.loads(row["payload"])["data"]["id"] in issues]
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
                    responses = retained_responses(self.journal, scope, operation)
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

    def run_once(self, handle, worker_id="linear-inbound", *, check_authority=None, task_ids=None):
        # The installed owner supplies its existing local enrollment/source
        # check. It is a consumption guard, never a new authority issuer.
        check_authority = check_authority or self.check_authority
        if check_authority is not None:
            check_authority()
        worker_id = f"{worker_id}:{uuid4()}"
        _, scope = self.core.session_binding(handle)
        receipt_id = self.next_receipt(handle, task_ids=task_ids)
        if receipt_id is None:
            return None
        receipt = self.journal.claim_delivery(scope, worker_id, self.clock(), receipt_id=receipt_id)
        if receipt is None:
            return None
        if receipt["id"] not in {row["id"] for row in self._scoped_deliveries(handle, [receipt])}:
            # Selection was scoped before claiming; refuse any changed identity
            # before applying, ignoring, retrying or finishing a provider receipt.
            from ai_task_manager.domains.work.errors import AccessDenied
            raise AccessDenied("Claimed inbound receipt changed successor task scope")
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
            # Journal acceptance can race the HTTP producer's Core admission,
            # including arrivals after the listener's startup repair snapshot.
            # Admit this claimed receipt using its original proof before any
            # classification or effect; a later producer admission is idempotent.
            self.core.sync.ingest(handle, receipt["delivery_id"], payload, receipt["ingress_ref"])
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
            try:
                snapshot = self.client.read_issue(task.issue_id)
                check()
                if snapshot.assignee_id != scope.principal:
                    raise ScopeMismatch(ownership_removal(snapshot))
                try:
                    self.client.require_delegate(snapshot)
                except LinearError as exc:
                    if exc.category != "agent_delegate_mismatch":
                        raise
                    raise ScopeMismatch(ownership_removal(snapshot)) from exc
            except ScopeMismatch as exc:
                check()
                if exc.observation:
                    record_scope_removal(
                        self.core,
                        self.journal,
                        handle,
                        task,
                        exc.observation,
                        f"linear-inbound-scope:{receipt['id']}:{exc.observation['updatedAt']}",
                        check_authority=check,
                    )
                raise
            checkpoint = self.journal.checkpoint(
                receipt["id"], worker_id, {"snapshot": asdict(snapshot)}, self.clock()
            )
            # Confirmation can commit in another process during the provider read.
            task = self.core.get_task(handle, task.id)
            matching_pending, responses, known_pending_echo = pending_echo(
                self.core, self.journal, handle, scope, task, snapshot
            )
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
            if self.journal.held_metadata(scope, task, snapshot, payload, self.authorized_actors):
                # Keep the durable signed delivery/checkpoint and acknowledgement,
                # without turning an unchanged label edit into fresh human intent.
                applied()
                return {"state": "applied", "classification": "held_metadata"}
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
