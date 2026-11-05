"""Retain current dependency observations while stopped-worker delivery consumes them."""

import threading
import time
from uuid import uuid4

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain


class DeliveryDependencies:
    def __init__(self, core, handle, source, deadline):
        self.core, self.handle, self.source, self.deadline = core, handle, source, deadline
        self.scope = core.session_binding(handle)
        self._bound = (core, handle, source, deadline)
        self._stop = threading.Event()
        self._thread = None
        self._error = None
        self._assignment = None
        self._graph = None

    def _facts(self):
        if (self.core, self.handle, self.source, self.deadline) != self._bound:
            raise AccessDenied("Delivery dependency owner changed")
        if self.source.core is not self.core or self.source.binding != self.scope:
            raise AccessDenied("Delivery dependency source belongs to another owner")
        if self.core.session_binding(self.handle) != self.scope:
            raise AccessDenied("Delivery dependency session changed")
        task_id, attempt_id, prose, revision = self._assignment
        task = self.core.get_task(self.handle, task_id)
        attempt = self.core.get_attempt(self.handle, attempt_id)
        if (
            self.core.context.clock() >= self.deadline
            or task.human_hold
            or task.prose_digest != prose
            or task.decision_revision != revision
            or attempt.task_id != task.id
            or attempt.session_id != self.scope[0]
            or attempt.state != "done"
        ):
            raise AccessDenied("Original delivery task, attempt or deadline changed")
        return task, attempt

    def _refresh(self):
        task, _ = self._facts()
        if task.delivered_once:
            self._stop.set()
            return
        before = self.core.scheduling.explain(self.handle, task.id)
        now = self.core.context.clock()
        if self._graph is None:
            self._graph = before["graph_version"]
        elif self._graph != before["graph_version"]:
            raise AccessDenied("Original delivery dependency graph changed")
        if before["state"] != "ready" or not before["expires_at"] or before["expires_at"] <= now:
            raise AccessDenied("Expired or unknown dependency authority cannot be revived")
        started = time.monotonic()
        key = "delivery-dependencies:" + str(uuid4())
        prepared = self.source.prepare(
            task.id, key, expected_version=task.version, decision_revision=task.decision_revision
        )
        self._facts()
        now = self.core.context.clock()
        if time.monotonic() - started > 20 or now >= min(before["expires_at"], self.deadline):
            raise AccessDenied("Dependency collection outlived its prior current authority")
        latest = self.core.scheduling.explain(self.handle, task.id)
        if latest["state"] != "ready" or latest["graph_version"] != before["graph_version"]:
            raise AccessDenied("Dependency graph changed while collecting delivery facts")
        after = self.core.scheduling.observe(
            self.handle, task.id, task.version, task.decision_revision, prepared.snapshot_ref, key
        )
        if after["state"] != "ready" or after["graph_version"] != before["graph_version"]:
            raise AccessDenied("Changed dependency facts require guarded reconciliation")
        # Observe can race an expiring prior graph while waiting for SQLite.
        # The existing Core check detects an invalidated original attempt; this
        # read-only boundary grants no command, renewal or publication.
        current, attempt = self._facts()
        with self.core.execution.worker_commit_boundary(
            self.handle,
            current.id,
            attempt.id,
            current.version,
            attempt.version,
            current.decision_revision,
        ):
            pass

    def start(self, row):
        assignment = (
            row["task_id"],
            row["attempt_id"],
            row["prose_digest"],
            row["decision_revision"],
        )
        if self._assignment is not None and assignment != self._assignment:
            raise AccessDenied("Delivery dependency task/attempt binding changed")
        self._assignment = assignment
        if self._thread is not None and self._thread.is_alive():
            self.check()
            return
        if self._error is not None:
            self.check()
        self._stop.clear()
        self._refresh()
        if self._stop.is_set():
            return
        self._thread = threading.Thread(target=self._run, name="delivery-dependencies", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(1):
            try:
                self._refresh()
            except Exception as error:
                self._error = error
                self._stop.set()
                return

    def check(self):
        if self._error is not None:
            raise AccessDenied("Delivery dependency observation failed") from self._error
        if self._assignment is not None:
            task, _ = self._facts()
            if task.delivered_once:
                return
            current = self.core.scheduling.explain(self.handle, task.id)
            if (
                current["state"] != "ready"
                or current["graph_version"] != self._graph
                or not current["expires_at"]
                or self.core.context.clock() >= current["expires_at"]
            ):
                raise AccessDenied("Delivery dependency authority is no longer current")

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(min(22, max(0, self.deadline - self.core.context.clock())))
            if thread.is_alive():
                raise RuntimeUncertain("Delivery dependency collector stop remains unknown")
