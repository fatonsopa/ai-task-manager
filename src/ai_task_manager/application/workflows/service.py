"""Trusted composition of the deterministic controller, never model-selected code."""

from ai_task_manager.application.service.models import result


class WorkflowService:
    def __init__(self, driver, task_plans=None, *, reviewer_owner=None):
        from types import MappingProxyType

        self.task_plans = MappingProxyType(dict(task_plans or {}))
        self.driver = driver
        self.reviewer_owner = reviewer_owner
        self.references = set()
        from threading import Lock

        self._registry_lock = Lock()
        self._task_locks = {}

    def check_owner_deadline(self, deadline):
        from ai_task_manager.domains.work.errors import AccessDenied

        if any(plan.deadline > deadline for plan in self.driver.plans._plans.values()):
            raise AccessDenied("Installed plan outlives the retained owner admission")

    def _resume(self, reference):
        with self._registry_lock:
            self.references.add(reference)
        observation = self.driver.run(reference)
        state = observation["state"]
        safe = {
            k: v
            for k, v in observation.items()
            if k
            in {
                "state",
                "reason",
                "workflow",
                "task_id",
                "artifact_digest",
                "required_units",
                "commit",
                "cleanup",
                "merge_workflow",
            }
        }
        envelope = result(
            safe,
            "confirmed" if state == "done" else "uncertain" if state == "uncertain" else "blocked",
        )
        if state != "done":
            envelope["exit_code"] = 6 if state == "uncertain" else 4
        return envelope

    def _ready(self):
        from ai_task_manager.application.service.models import Unavailable

        if any(
            value is None
            for value in (self.driver.runtime, self.driver.repository, self.driver.provider)
        ):
            raise Unavailable(
                "Required workflow worker, repository or provider port is unavailable"
            )

    def run(self, task_id, plan_ref, operation_key):
        self._ready()
        with self._task_lock(task_id):
            prior = self.driver.store.find(self.driver.scope, task_id, operation_key)
            if prior is None and self.task_plans.get(task_id) != plan_ref:
                from ai_task_manager.domains.work.errors import AccessDenied

                raise AccessDenied("The installed plan is not assigned to this task")
            reference = self.driver.begin(task_id, plan_ref, operation_key)
            return self._resume(reference)

    def recover_expired(self, authorization):
        """Trusted code-only admission; this deliberately does not execute the run."""
        from ai_task_manager.domains.work.errors import AccessDenied

        from .recovery import RecoveryAuthorization, recover_expired

        if type(authorization) is not RecoveryAuthorization:
            raise AccessDenied("Trusted typed recovery authorization is required")
        self._ready()
        if self.task_plans.get(authorization.task_id) != authorization.plan_ref:
            raise AccessDenied("The recovery plan is not assigned to this task")
        with self._task_lock(authorization.task_id):
            return recover_expired(self.driver, authorization)

    def inspect(self, workflow_ref):
        from .inspection import inspect_row

        row = self.driver.store.read(workflow_ref, self.driver.scope)
        if self.task_plans.get(row["task_id"]) != row["plan_id"]:
            from ai_task_manager.domains.work.errors import AccessDenied

            raise AccessDenied("The installed plan is not assigned to this task")
        return inspect_row(
            self.driver.core, self.driver.handle, row,
            self.driver.store.original_key(workflow_ref, self.driver.scope),
        )

    def resume(self, workflow_ref):
        self._ready()
        row = self.driver.store.read(workflow_ref, self.driver.scope)
        with self._task_lock(row["task_id"]):
            return self._resume(workflow_ref)

    def _task_lock(self, task_id):
        from contextlib import contextmanager
        from threading import Lock

        from ai_task_manager.supervisor.models import RuntimeRefused

        with self._registry_lock:
            lock = self._task_locks.setdefault(task_id, Lock())

        @contextmanager
        def claimed():
            if not lock.acquire(timeout=120):
                raise RuntimeRefused("Current task request is still owned")
            try:
                yield
            finally:
                lock.release()

        return claimed()

    def close(self):
        """Request stop only through retained actual owners; never infer quiescence."""
        from uuid import uuid4

        from ai_task_manager.domains.work.errors import EvidenceRequired
        from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS

        outcomes = []
        driver = self.driver
        for reference in sorted(self.references):
            owner = str(uuid4())
            claimed = False
            try:
                driver.store.claim(reference, driver.scope, owner)
                claimed = True
                row = driver.store.read(reference, driver.scope)
                task = driver.core.get_task(driver.handle, row["task_id"])
                if not row["attempt_id"]:
                    pending = row["steps"].get("start")
                    if pending and pending["result"] is None:
                        observed = driver._reconcile(row, task, "start", pending)
                        if observed is None:
                            raise EvidenceRequired(
                                "Unresolved startup retains its owner obligation"
                            )
                        driver.store.finish(
                            reference, owner, "start", observed, attempt_id=observed["attempt_id"]
                        )
                        row = driver.store.read(reference, driver.scope)
                    else:
                        continue
                attempt = driver.core.get_attempt(driver.handle, row["attempt_id"])
                if attempt.task_id != task.id:
                    raise EvidenceRequired("Known task and attempt ownership required")
                prior = row["steps"].get("owner-stop")
                if prior is not None:
                    if prior["result"] is not None:
                        outcomes.append({"workflow": reference, **prior["result"]})
                        continue
                    events = driver.core.notifications.list_events(
                        driver.handle, prior["payload"]["cursor"]
                    )
                    found = None
                    for event in events:
                        if (
                            event.task_id != task.id
                            or event.kind != "attempt_outcome_recorded"
                            or event.payload.get("attempt_id") != attempt.id
                            or event.payload.get("outcome") != attempt.state
                        ):
                            continue
                        proof = driver.core.context.proof(
                            event.payload.get("outcome_ref"),
                            "attempt_outcome",
                            driver.scope,
                            attempt.id,
                        )
                        if (
                            proof.payload.get("quiescent") is True
                            and proof.payload.get("state") == attempt.state
                            and attempt.state not in ACTIVE_ATTEMPTS
                        ):
                            found = {
                                "attempt_id": attempt.id,
                                "state": attempt.state,
                                "reconciled_from_core_event": event.id,
                            }
                    if found is None:
                        raise EvidenceRequired("Prior stop remains uncertain; never replay it")
                    driver.store.finish(reference, owner, "owner-stop", found)
                    outcomes.append({"workflow": reference, **found})
                    continue
                if attempt.state not in ACTIVE_ATTEMPTS:
                    started = row["steps"].get("start", {}).get("result") or {}
                    if (
                        started.get("failed_start") is True
                        and started.get("attempt_id") == attempt.id
                        and started.get("state") == attempt.state == "failed"
                        and started.get("quiescent") is True
                    ):
                        outcomes.append({"workflow": reference, **started})
                    continue
                pair = driver.runtime.pair(attempt.id)
                if (
                    pair.attempt_id != attempt.id
                    or pair.worktree_id != attempt.worktree_id
                    or pair.database_id != attempt.database_id
                ):
                    raise EvidenceRequired("Retained resource ownership differs")
                cursor = max(
                    (e.sequence for e in driver.core.notifications.list_events(driver.handle)),
                    default=0,
                )
                driver.store.intend(
                    reference,
                    owner,
                    "owner-stop",
                    {
                        "attempt_id": attempt.id,
                        "operation_key": reference + ":owner-stop",
                        "cursor": cursor,
                    },
                )
                driver.runtime.stop(driver.handle, attempt, reference + ":owner-stop")
                observed = driver.core.get_attempt(driver.handle, attempt.id)
                driver.store.finish(
                    reference,
                    owner,
                    "owner-stop",
                    {"attempt_id": attempt.id, "state": observed.state},
                )
                outcomes.append({"workflow": reference, "state": observed.state})
            except Exception as error:
                outcomes.append(
                    {"workflow": reference, "state": "uncertain", "reason": type(error).__name__}
                )
            finally:
                if claimed:
                    driver.store.release(reference, owner)
        if getattr(driver, "code_delivery", None) is not None:
            try:
                outcomes.extend(driver.code_delivery.close())
            except Exception as error:
                outcomes.append({"state": "uncertain", "reason": type(error).__name__})
        if self.reviewer_owner is not None:
            try:
                outcomes.extend(self.reviewer_owner.close())
            except Exception as error:
                outcomes.append(
                    {
                        "role": "reviewer",
                        "state": "uncertain",
                        "quiescent": False,
                        "reason": type(error).__name__,
                    }
                )
        return outcomes
