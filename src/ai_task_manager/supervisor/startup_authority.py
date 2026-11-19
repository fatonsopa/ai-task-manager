"""Maintain dependency observations while one owned native worker is starting.

This helper grants no work lease or authentication. Provider collection remains
outside Core transactions; an expired or invalidated attempt cannot be revived.
"""

import threading
import time
from contextlib import nullcontext
from time import monotonic as diagnostic_clock
from uuid import uuid4

from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import AttemptState
from ai_task_manager.observability.failures import FailureStage, capture_failure
from ai_task_manager.storage.sessions import TaskSession
from ai_task_manager.supervisor.authority import CHECK_INTERVAL_SECONDS, refresh_schedule


class StartupAuthority:
    def __init__(self, registry, handle, attempt, runtime, deadline):
        self.registry, self.handle, self.attempt_id = registry, handle, attempt.id
        self.runtime, self.deadline = runtime, deadline
        self.owner = runtime.attempt(attempt.id)
        self.task_id = attempt.task_id
        self.session, self.scope = registry.core.session_binding(handle)
        task = registry.core.get_task(handle, self.task_id)
        self.task_binding = (task.issue_id, task.prose_digest, task.decision_revision)
        self._bound = (registry, handle, runtime, deadline)
        self._stop = threading.Event()
        self._thread = None
        self._failure = None
        self._handover = False
        self._latency = 0.0
        self._preparation_latency = 0.0
        self._dependency_ref = None
        self.failure_diagnostic = None
        self.check()

    def _check_owner(self):
        self.registry.check()
        core = self.registry.core
        session, scope = core.session_binding(self.handle)
        task = core.get_task(self.handle, self.task_id)
        attempt = core.get_attempt(self.handle, self.attempt_id)
        if (
            (self.registry, self.handle, self.runtime, self.deadline) != self._bound
            or (session, scope) != (self.session, self.scope)
            or self.runtime.attempt(self.attempt_id) != self.owner
            or self.owner["deadline"] != self.deadline
            or self.owner["task_id"] != self.task_id
            or self.owner["host"] != attempt.host
            or self.owner["scope"] != vars(self.scope)
            or attempt.task_id != self.task_id
            or (attempt.worker_id is not None and attempt.worker_id != self.owner["worker_id"])
            or attempt.state not in {AttemptState.RESERVED, AttemptState.STARTING}
            or (task.issue_id, task.prose_digest, task.decision_revision) != self.task_binding
            or task.human_hold
            or task.delivered_once
            or task.sync_state != "confirmed"
            or (
                task.observed_status != "Todo"
                and not (
                    isinstance(session, TaskSession)
                    and self.task_id in session.task_ids
                    and task.observed_status == "In Progress"
                )
            )
            or core.context.clock() >= self.deadline
        ):
            raise AccessDenied("Startup no longer has its original bounded task owner")
        if self._failure is not None:
            raise AccessDenied("Startup dependency observation failed") from self._failure
        return task

    def check(self):
        task = self._check_owner()
        if self.registry.core.scheduling.explain(self.handle, self.task_id)["state"] != "ready":
            raise SchedulingBlocked("Startup dependencies expired or were invalidated")
        return task

    def refresh(self):
        started = diagnostic_clock()
        core_started = self.registry.core.context.clock()
        boundary = getattr(self.registry.dependencies, "publication_boundary", None)
        with (
            boundary(self.task_id, not_after=self.deadline, cancelled=self._stop)
            if callable(boundary) else nullcontext()
        ):
            waiting = (diagnostic_clock() - started,
                       self.registry.core.context.clock() - core_started)
            if not all(elapsed >= 0 for elapsed in waiting):
                raise AccessDenied("Startup preparation clock moved backwards")
            return self._refresh(max(waiting))

    def _refresh(self, preparation_latency=0.0):
        if self._stop.is_set():
            raise AccessDenied("Startup dependency collection is stopped")
        task = self.check()
        key = "startup-dependencies:" + str(uuid4())
        started = time.monotonic()
        # Actual HTTP and complete source observations occur outside Core.
        value = self.registry.dependencies.prepare(
            task.id,
            key,
            expected_version=task.version,
            decision_revision=task.decision_revision,
        )
        self._latency = max(self._latency, time.monotonic() - started)
        prepared_at = diagnostic_clock()
        prepared_clock = self.registry.core.context.clock()
        if self._stop.is_set() and not self._handover:
            return
        # Recheck the prior authority before consuming a new observation.
        self.check()
        core = self.registry.core
        core.scheduling.observe(
            self.handle,
            task.id,
            task.version,
            task.decision_revision,
            value.snapshot_ref,
            key + ":commit",
        )
        self._dependency_ref = value.snapshot_ref
        self.check()
        local_times = (diagnostic_clock() - prepared_at,
                       core.context.clock() - prepared_clock)
        if not all(elapsed >= 0 for elapsed in local_times):
            raise AccessDenied("Startup preparation clock moved backwards")
        self._preparation_latency = max(
            self._preparation_latency, preparation_latency + max(local_times),
        )

    def start(self):
        if self._thread is not None or self._stop.is_set():
            raise AccessDenied("Startup observer cannot be restarted")
        self.check()

        def run():
            next_at = self.registry.core.context.clock()
            while not self._stop.is_set():
                started = diagnostic_clock()
                try:
                    self._check_owner()
                    prepare = getattr(
                        self.registry.dependencies, "prepare_current_milestones", None
                    )
                    if self._dependency_ref is not None and callable(prepare):
                        boundary = getattr(self.registry.dependencies, "publication_boundary", None)
                        with (
                            boundary(self.task_id, not_after=self.deadline, cancelled=self._stop)
                            if callable(boundary) else nullcontext()
                        ):
                            self._check_owner()
                            prepare(self._dependency_ref, task_id=self.task_id)
                    self.check()
                    if self.registry.core.context.clock() >= next_at:
                        self.refresh()
                        view = self.registry.core.scheduling.explain(self.handle, self.task_id)
                        # Keep the existing costly-collection refusal, and
                        # reserve the full next collection even after a fast one.
                        margin = max(5, 2 * self._latency + 1)
                        if view["expires_at"] - self.registry.core.context.clock() <= margin:
                            raise SchedulingBlocked(
                                "Startup collection leaves no safe next freshness window"
                            )
                        next_at = min(
                            view["expires_at"] - margin,
                            refresh_schedule(
                                view["expires_at"], self.deadline,
                                self.registry.core.context.clock(), self._preparation_latency,
                            ),
                        )
                except BaseException as error:
                    if not self._stop.is_set():
                        self._failure = error
                        failure = capture_failure(
                            error, FailureStage.STARTUP_MONITOR, started=started
                        )
                        self.failure_diagnostic = {"failure": failure, "retained": False}
                        try:
                            reference = self.runtime.observe(
                                self.attempt_id, "startup_failure_cause", {"failure": failure}
                            )
                            self.failure_diagnostic.update(retained=True, reference=reference)
                        except BaseException as retention_error:
                            self.failure_diagnostic["retention_failure"] = capture_failure(
                                retention_error, FailureStage.DIAGNOSTIC_RETENTION
                            )
                    return
                if self._stop.wait(CHECK_INTERVAL_SECONDS):
                    return

        self._thread = threading.Thread(
            target=run,
            name="managed-startup-dependencies",
            daemon=True,
        )
        self._thread.start()

    def close(self):
        self.request_close()
        return self._stop_and_join()

    def request_close(self):
        self._handover = False
        self._stop.set()

    def _stop_and_join(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            remaining = max(0, self.deadline - self.registry.core.context.clock())
            self._thread.join(timeout=min(22, remaining))
        return self._thread is None or not self._thread.is_alive()

    def finish(self):
        self._handover = True
        if not self._stop_and_join():
            raise AccessDenied("Startup dependency collector stop remains unknown")
        self.check()
