"""Retain current dependency observations while stopped-worker delivery consumes them."""

import threading
import time
from dataclasses import replace
from uuid import uuid4

from ai_task_manager.adapters.linear.dependencies.source import Preparation
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion
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
        self._mode = "idle"
        self._publication = None
        self._row = None

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
        if task.observed_status != "In Progress" or task.sync_state != "confirmed":
            raise AccessDenied("Delivery dependency observation requires confirmed current work")
        before = self.core.scheduling.explain(self.handle, task.id)
        now = self.core.context.clock()
        if self._graph is None:
            self._graph = before["graph_version"]
        elif self._graph != before["graph_version"]:
            raise AccessDenied("Original delivery dependency graph changed")
        if before["state"] != "ready" or not before["expires_at"] or before["expires_at"] <= now:
            raise AccessDenied("Expired or unknown dependency authority cannot be revived")
        started = time.monotonic()
        collection_end = min(before["expires_at"], self.deadline)
        after = None
        for _ in range(3):
            task, _ = self._facts()
            self._collection_current(before, started, collection_end)
            key = "delivery-dependencies:" + str(uuid4())
            try:
                prepared = self.source.prepare(
                    task.id,
                    key,
                    expected_version=task.version,
                    decision_revision=task.decision_revision,
                )
                if self._stop.is_set():
                    return
                self._collection_current(before, started, collection_end)
                if type(prepared) is not Preparation or (
                    prepared.task_id,
                    prepared.task_version,
                    prepared.decision_revision,
                    prepared.operation_key,
                ) != (task.id, task.version, task.decision_revision, key):
                    raise AccessDenied("Dependency receipt has another exact preparation context")
                after = self.core.scheduling.observe(
                    self.handle,
                    prepared.task_id,
                    prepared.task_version,
                    prepared.decision_revision,
                    prepared.snapshot_ref,
                    prepared.operation_key,
                )
                break
            except StaleVersion:
                if self._stop.is_set():
                    return
                current, _ = self._facts()
                # Only this workflow's monotonic unit progress permits a new
                # collection. The old-version receipt is never rebound/reused.
                if (
                    current == task
                    or replace(task, version=current.version, completed_work=current.completed_work)
                    != current
                    or not set(task.completed_work) < set(current.completed_work)
                ):
                    raise AccessDenied("Foreign task or dependency changes cannot be recollected")
                self._collection_current(before, started, collection_end)
        if after is None:
            raise AccessDenied("Bounded own-unit dependency recollection was exhausted")
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

    def _collection_current(self, before, started, end):
        current, _ = self._facts()
        if current.observed_status != "In Progress" or current.sync_state != "confirmed":
            raise AccessDenied("Task synchronization changed during dependency collection")
        if time.monotonic() - started > 20 or self.core.context.clock() >= end:
            raise AccessDenied("Dependency collection outlived its original current authority")
        latest = self.core.scheduling.explain(self.handle, current.id)
        if latest["state"] != "ready" or latest["graph_version"] != before["graph_version"]:
            raise AccessDenied("Dependency graph changed while collecting delivery facts")

    def bind(self, row):
        assignment = (
            row["task_id"],
            row["attempt_id"],
            row["prose_digest"],
            row["decision_revision"],
        )
        if self._assignment is not None and assignment != self._assignment:
            raise AccessDenied("Delivery dependency task/attempt binding changed")
        self._assignment, self._row = assignment, row
        self._facts()

    def start(self, row=None):
        self.bind(row if row is not None else self._row)
        if self._mode == "publication":
            self.check()
            return
        if self._thread is not None and self._thread.is_alive():
            self.check()
            return
        if self._error is not None:
            self.check()
        self._stop.clear()
        self._mode = "observing"
        self._refresh()
        if self._stop.is_set():
            return
        self._thread = threading.Thread(target=self._run, name="delivery-dependencies", daemon=True)
        self._thread.start()

    def before_fence(self, plan):
        if (
            self._assignment is None
            or (plan.grant.binding.task_id, plan.grant.binding.attempt_id) != self._assignment[:2]
        ):
            raise AccessDenied("Publication handoff belongs to another delivery")
        self.close()
        if self._error is not None:
            raise AccessDenied(
                "Failed dependency observation cannot admit publication"
            ) from self._error
        self._facts()
        # No generic issue-open lookup is permitted after Core has inserted the
        # exact fence. New begin operations atomically require the current graph;
        # identical committed begin operations retain their historical replay.
        self._mode = "handoff"

    def adopt_fence(self, plan, context, binding_digest, commit):
        if context.binding_digest != binding_digest or context.commit != commit:
            raise AccessDenied("Publication context differs from the exact selected binding")
        self.before_fence(plan)
        fact = self.core.publications.inspect(self.handle, plan.grant.binding.task_id, context)
        intent = fact["intent"]
        g = plan.grant
        expected = {
            "task_id": g.binding.task_id,
            "attempt_id": g.binding.attempt_id,
            "prose_digest": g.binding.prose_digest,
            "task_version": g.task_version,
            "attempt_version": g.attempt_version,
            "decision_revision": g.decision_revision,
            "decision_ref": g.decision_ref,
            "source_ref": g.source_ref,
            "source_head": g.source_head,
            "old": g.target_head,
            "binding_digest": binding_digest,
            "commit": commit,
        }
        if any(intent.get(key) != value for key, value in expected.items()):
            raise AccessDenied("Canonical publication intent belongs to another source or grant")
        selected = (plan, context, binding_digest, commit)
        if self._publication is not None and self._publication != selected:
            raise AccessDenied("Retained publication handoff changed")
        self._publication, self._mode = selected, "publication"
        self.check()

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
        if self._assignment is None:
            return
        task, _ = self._facts()
        if self._mode == "publication":
            plan, context, _, _ = self._publication
            fact = self.core.publications.inspect(self.handle, task.id, context)
            if fact["state"] == "pending":
                g = plan.grant
                with self.core.execution.merge_snapshot_boundary(
                    self.handle,
                    task.id,
                    g.binding.attempt_id,
                    g.task_version,
                    g.attempt_version,
                    g.decision_revision,
                    g.decision_ref,
                    publication_context=context,
                ):
                    pass
            # Committed canonical facts authorize only receipt consumption here.
            # The existing publication consumer refuses any new Git effect.
            return
        if self._mode in {"idle", "handoff"} or task.delivered_once:
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
