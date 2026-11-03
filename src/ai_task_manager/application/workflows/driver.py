"""Derive mechanical actions from Core facts and durable intents, not model memory."""

from uuid import uuid4

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS, Cause, IssueStatus


class WorkflowDriver:
    def __init__(
        self,
        core,
        handle,
        store,
        plans,
        *,
        runtime=None,
        repository=None,
        provider=None,
        decisions=None,
        reviews=None,
        clock=None,
        faults=None,
    ):
        self.core, self.handle, self.store, self.plans = core, handle, store, plans
        self.runtime, self.repository, self.provider = runtime, repository, provider
        self.decisions, self.reviews, self.faults = decisions, reviews, faults
        self.clock = clock or core.context.clock
        self.session_id, self.scope = core.session_binding(handle)

    def begin(self, task_id, plan_ref, operation_key):
        task = self.core.get_task(self.handle, task_id)
        plan = self.plans.get(plan_ref)
        binding = self.runtime.binding(task, plan) if self.runtime else None
        if self.store.find(self.scope, task.id, operation_key) is not None:
            return self.store.create(self.scope, task, plan, operation_key, binding)
        if (
            task.code_bearing
            or task.observed_status != IssueStatus.TODO
            or task.sync_state != "confirmed"
        ):
            raise EvidenceRequired("First workflow slice requires confirmed non-code Todo")
        if plan.deadline <= self.clock():
            raise AccessDenied("Original workflow deadline expired")
        return self.store.create(self.scope, task, plan, operation_key, binding)

    def _context(self, ref):
        if self.core.session_binding(self.handle) != (self.session_id, self.scope):
            raise AccessDenied("Workflow session binding changed")
        row = self.store.read(ref, self.scope)
        task = self.core.get_task(self.handle, row["task_id"])
        plan = self.plans.get(row["plan_id"])
        if row["plan_digest"] != plan.digest or task.prose_digest != row["prose_digest"]:
            raise AccessDenied("Trusted plan or approved task scope changed")
        if self.runtime and self.runtime.binding(task, plan) != row["runtime_digest"]:
            raise AccessDenied("Registered execution changed")
        return row, task, plan

    def _attempt(self, row):
        if not row["attempt_id"]:
            return None
        attempt = self.core.get_attempt(self.handle, row["attempt_id"])
        if attempt.task_id != row["task_id"] or attempt.host != self.plans.get(row["plan_id"]).host:
            raise AccessDenied("Canonical attempt does not belong to this task and configured host")
        return attempt

    def _decision(self, task, target, action):
        if self.decisions is None:
            raise EvidenceRequired("Decision authority is not configured")
        return self.decisions(self.scope, task, target, action)

    def _reconcile_start(self, row, task, intent):
        cursor = intent["payload"]["cursor"]
        events = self.core.notifications.list_events(self.handle)
        if (
            type(cursor) is not int
            or cursor < 0
            or cursor > max((event.sequence for event in events), default=0)
        ):
            return None
        reservations = [
            event
            for event in events
            if event.task_id == task.id and event.kind == "attempt_reserved"
        ]
        ids = {event.payload["attempt_id"] for event in reservations if event.sequence > cursor}
        if len(ids) == 1:
            attempt = self.core.get_attempt(self.handle, next(iter(ids)))
            if attempt.state == "running":
                self.runtime.pair(attempt.id)
                return {"attempt_id": attempt.id, "reconciled": True}
        original = next(
            (
                event
                for event in reversed(events)
                if event.task_id == task.id and event.sequence <= cursor
            ),
            None,
        )
        if (
            reservations
            or row["attempt_id"] is not None
            or self.runtime is None
            or task.execution_state != "idle"
            or task.observed_status != IssueStatus.TODO
            or task.sync_state != "confirmed"
            or original is None
            or original.payload.get("task_version") != task.version
            or original.payload.get("decision_revision") != task.decision_revision
        ):
            return None
        # Core writes reservation and its event atomically. With no reservation
        # and the original task version, retry the unchanged intent and operation.
        attempt = self.runtime.start(
            self.handle,
            task,
            self.plans.get(row["plan_id"]),
            intent["payload"]["decision_ref"],
            row["id"] + ":start",
        )
        return {"attempt_id": attempt.id, "reconciled": True}

    def _reconcile(self, row, task, name, intent):
        attempt = self._attempt(row)
        if name == "start":
            return self._reconcile_start(row, task, intent)
        elif (
            name == "execute"
            and attempt
            and attempt.state in {"done", "failed", "paused", "canceled"}
        ):
            self.runtime.pair(attempt.id)
            return {"attempt_id": attempt.id, "state": attempt.state, "reconciled": True}
        elif name == "verify":
            return self.repository.inspect(task, attempt, self.plans.get(row["plan_id"]))
        elif name.startswith("unit:"):
            unit = name[5:]
            if unit in task.completed_work:
                matches = [
                    e
                    for e in self.core.notifications.list_events(self.handle)
                    if e.task_id == task.id
                    and e.kind == "work_unit_verified"
                    and e.payload.get("unit") == unit
                    and e.payload.get("proof_ref") == intent["payload"]["proof_ref"]
                ]
                if matches:
                    return {"unit": unit, "reconciled": True}
        elif name == "deliver" and task.desired_status == IssueStatus.DONE:
            matches = [
                e
                for e in self.core.notifications.list_events(self.handle)
                if e.task_id == task.id
                and e.kind == "transition_requested"
                and e.payload.get("decision_ref") == intent["payload"]["decision_ref"]
            ]
            if matches:
                return {"desired": "Done", "reconciled": True}
        elif name == "confirm" and task.sync_state == "confirmed":
            return {"observed": task.observed_status, "reconciled": True}
        return None

    def run(self, ref):
        owner = str(uuid4())
        self.store.claim(ref, self.scope, owner)
        try:
            for _ in range(self._context(ref)[2].max_steps):
                row, task, plan = self._context(ref)
                attempt = self._attempt(row)
                if task.human_hold or task.decision_revision != row["decision_revision"]:
                    if attempt and attempt.state in ACTIVE_ATTEMPTS and self.runtime is not None:
                        self.runtime.stop(self.handle, attempt, ref + ":stop")
                    return {
                        "state": "blocked",
                        "reason": "human_or_assignment_change",
                        "workflow": ref,
                    }
                if plan.deadline <= self.clock():
                    return {"state": "checkpoint", "reason": "invocation_deadline", "workflow": ref}
                pending = [(n, s) for n, s in row["steps"].items() if s["result"] is None]
                if pending:
                    name, intent = pending[0]
                    result = self._reconcile(row, task, name, intent)
                    if result is None:
                        return {
                            "state": "uncertain",
                            "reason": "unfinished_" + name,
                            "workflow": ref,
                        }
                    self.store.finish(ref, owner, name, result, attempt_id=result.get("attempt_id"))
                    continue
                if task.observed_status == IssueStatus.DONE and task.sync_state == "confirmed":
                    if "deliver" in row["steps"] and set(task.required_work) <= set(
                        task.completed_work
                    ):
                        return {"state": "done", "workflow": ref, "task_id": task.id}
                    return {
                        "state": "blocked",
                        "reason": "external_terminal_change",
                        "workflow": ref,
                    }
                if self.runtime is None or self.repository is None:
                    return {
                        "state": "blocked",
                        "reason": "worker_or_repository_port_unavailable",
                        "workflow": ref,
                    }
                if (
                    task.desired_status == IssueStatus.DONE
                    and task.sync_state != "confirmed"
                    and "deliver" in row["steps"]
                ):
                    if self.provider is None:
                        raise EvidenceRequired("Provider confirmation is unavailable")
                    name, payload = "confirm", {"desired": task.desired_status}

                    def action():
                        return self.provider.confirm_pending(self.handle, task.id)
                elif attempt is None:
                    if task.observed_status != IssueStatus.TODO or task.sync_state != "confirmed":
                        return {
                            "state": "blocked",
                            "reason": "confirmed_queue_required",
                            "workflow": ref,
                        }
                    decision = self._decision(task, "In Progress", "start")
                    events = self.core.notifications.list_events(self.handle)
                    payload = {
                        "cursor": max((e.sequence for e in events), default=0),
                        "decision_ref": decision,
                    }
                    name = "start"

                    def action():
                        return self.runtime.start(self.handle, task, plan, decision, ref + ":start")
                elif attempt.state == "running" and "execute" not in row["steps"]:
                    pair = self.runtime.pair(attempt.id)
                    if pair.database in {self.store.path, self.core.context.db.path}:
                        raise AccessDenied("Task database cannot contain controller metadata")
                    lineage = self.runtime.ownership(attempt.id)
                    self.store.lineage(task.id, attempt.id, lineage)
                    name, payload = (
                        "execute",
                        {"attempt_id": attempt.id, "command_ref": plan.command_ref},
                    )

                    def action():
                        return self.runtime.execute(self.handle, attempt, plan, ref + ":execute")
                elif attempt.state == "done":
                    lineage = self.runtime.ownership(attempt.id)
                    self.store.lineage(task.id, attempt.id, lineage)
                    current = self.repository.inspect(task, attempt, plan)
                    if "verify" not in row["steps"]:
                        name, payload = "verify", {"attempt_id": attempt.id}

                        def action():
                            return current
                    else:
                        saved = self.store.verified(self.scope, task.id, current["digest"])
                        if saved is None:
                            return {
                                "state": "awaiting_review",
                                "reason": "artifact_revision",
                                "workflow": ref,
                            }
                        refs = (
                            self.reviews.read(self.scope, task, current) if self.reviews else None
                        )
                        if refs is None:
                            return {
                                "state": "awaiting_review",
                                "reason": "semantic_review_unavailable",
                                "workflow": ref,
                                "artifact_digest": current["digest"],
                                "required_units": list(task.required_work),
                            }
                        missing = [u for u in task.required_work if u not in task.completed_work]
                        if missing:
                            unit = missing[0]
                            name = "unit:" + unit
                            payload = {"proof_ref": refs["units"][unit]}

                            def action():
                                return self.core.execution.complete_unit(
                                    self.handle,
                                    task.id,
                                    unit,
                                    task.version,
                                    task.decision_revision,
                                    refs["units"][unit],
                                    ref + ":" + name,
                                )
                        elif task.sync_state != "confirmed":
                            if self.provider is None:
                                raise EvidenceRequired("Provider confirmation is unavailable")
                            name, payload = "confirm", {"desired": task.desired_status}

                            def action():
                                return self.provider.confirm_pending(self.handle, task.id)
                        elif task.observed_status == IssueStatus.IN_PROGRESS:
                            decision = self._decision(task, "Done", "non_code_complete")
                            name, payload = (
                                "deliver",
                                {"decision_ref": decision, "proof_ref": refs["delivery"]},
                            )

                            def action():
                                return self.core.transitions.transition(
                                    self.handle,
                                    task.id,
                                    IssueStatus.DONE,
                                    Cause.NON_CODE_COMPLETE,
                                    task.version,
                                    task.decision_revision,
                                    decision,
                                    {"non_code_completion": refs["delivery"]},
                                    ref + ":deliver",
                                )
                        else:
                            return {
                                "state": "blocked",
                                "reason": "unsupported_delivery_state",
                                "workflow": ref,
                            }
                else:
                    return {
                        "state": "blocked",
                        "reason": "attempt_" + attempt.state,
                        "workflow": ref,
                    }
                self.store.intend(ref, owner, name, payload)
                if self.faults:
                    self.faults.hit("before:" + name)
                result = action()
                if self.faults:
                    self.faults.hit("after:" + name)
                if name == "start":
                    receipt = {"attempt_id": result.id}
                elif name == "execute":
                    receipt = {
                        "attempt_id": attempt.id,
                        "state": self.core.get_attempt(self.handle, attempt.id).state,
                    }
                elif name == "verify":
                    receipt = result
                else:
                    receipt = {"task_version": result.version}
                self.store.finish(ref, owner, name, receipt, attempt_id=receipt.get("attempt_id"))
            return {"state": "checkpoint", "reason": "step_limit", "workflow": ref}
        except Exception as error:
            return {
                "state": "blocked",
                "reason": type(error).__name__,
                "detail": str(error),
                "workflow": ref,
            }
        finally:
            self.store.release(ref, owner)
