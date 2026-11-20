"""Retain current dependency observations while stopped-worker delivery consumes them."""

import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from uuid import uuid4

from ai_task_manager.adapters.linear.dependencies.source import (
    DependencyPublicationCancelled,
    Preparation,
)
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion
from ai_task_manager.supervisor.authority import (
    CHECK_INTERVAL_SECONDS,
    MAX_COLLECTION_SECONDS,
    refresh_schedule,
)
from ai_task_manager.supervisor.models import RuntimeUncertain


class DeliveryDependencies:
    def __init__(self, core, handle, source, deadline, *, recovery=None):
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
        self._next_refresh_at = None
        self._preparation_latency = 0.0
        self.recovery, self._recovery_bound = recovery, recovery
        self._completed_commits = None
        self._cold_ref, self._cold_bound = None, False

    def bind_commits(self, commits):
        if self._completed_commits is not None and self._completed_commits is not commits:
            raise AccessDenied("Completed delivery commit owner changed")
        self._completed_commits = commits

    def _facts(self):
        if self.recovery is not self._recovery_bound:
            raise AccessDenied("Cold delivery recovery owner changed")
        if (self.core, self.handle, self.source, self.deadline) != self._bound:
            raise AccessDenied("Delivery dependency owner changed")
        if self.source.core is not self.core or self.source.binding != self.scope:
            raise AccessDenied("Delivery dependency source belongs to another owner")
        if self.core.session_binding(self.handle) != self.scope:
            raise AccessDenied("Delivery dependency session changed")
        task_id, attempt_id, prose, revision = self._assignment
        task = self.core.get_task(self.handle, task_id)
        if self._cold_ref is not None:
            self.core.context.proof(
                self._cold_ref, "workflow_cold_delivery", self.scope[1], task.id
            )
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
        from ai_task_manager.storage.sessions import TaskSession

        task, _ = self._facts()
        current = self.core.scheduling.explain(self.handle, task.id)
        pending_commit = self._row.get("steps", {}).get("worker-commit")
        from ai_task_manager.storage import scheduling

        with self.core.context.db.transaction() as conn:
            binding = scheduling.attempt_binding(conn, self._assignment[1])
        needs_recovery = (
            current["state"] != "ready" or not current["expires_at"]
            or current["expires_at"] <= self.core.context.clock()
            or bool(binding and binding["invalidated_by"])
        )
        if (self.recovery is not None and self._cold_ref is None and needs_recovery
                and pending_commit is not None and pending_commit["result"] is None):
            from .cold import ColdRecovery

            if type(self.recovery) is not ColdRecovery:
                raise AccessDenied("Verified original cold recovery is required")
            self._cold_ref = self.recovery.prepare_delivery(
                self.core, self.handle, self._row, self._completed_commits
            )
        successor = (
            isinstance(self.scope[0], TaskSession)
            and self._row.get("succession", {}).get("version") == 2
        )
        end = self.deadline if successor or (self._cold_ref and not self._cold_bound) else min(
            self.deadline, current["expires_at"] or self.deadline
        )
        boundary = getattr(self.source, "publication_boundary", None)
        with (
            boundary(task.id, not_after=end, cancelled=self._stop)
            if callable(boundary) else nullcontext()
        ):
            return self._refresh_current()

    def _refresh_current(self):
        task, _ = self._facts()
        if task.delivered_once:
            self._stop.set()
            return
        if task.observed_status != "In Progress" or task.sync_state != "confirmed":
            raise AccessDenied("Delivery dependency observation requires confirmed current work")
        from ai_task_manager.storage.sessions import TaskSession

        successor = (
            isinstance(self.scope[0], TaskSession)
            and self._row.get("succession", {}).get("version") == 2
        )
        before = self.core.scheduling.explain(self.handle, task.id)
        now = self.core.context.clock()
        if self._graph is None:
            self._graph = before["graph_version"]
        elif self._graph != before["graph_version"]:
            raise AccessDenied("Original delivery dependency graph changed")
        bootstrap = (successor and self._graph == before["graph_version"]) or (
            self._cold_ref is not None and not self._cold_bound
        )
        if not bootstrap:
            self.prepare_local()
            before = self.core.scheduling.explain(self.handle, task.id)
            now = self.core.context.clock()
        if (
            before["state"] != "ready" or not before["expires_at"] or before["expires_at"] <= now
        ) and not bootstrap:
            raise AccessDenied("Expired or unknown dependency authority cannot be revived")
        started = time.monotonic()
        collection_end = self.deadline if bootstrap else min(before["expires_at"], self.deadline)
        after = None
        for _ in range(3):
            task, _ = self._facts()
            self._collection_current(before, started, collection_end, bootstrap=bootstrap)
            key = "delivery-dependencies:" + str(uuid4())
            try:
                prepared = self.source.prepare(
                    task.id,
                    key,
                    expected_version=task.version,
                    decision_revision=task.decision_revision,
                )
                preparation_started = time.monotonic()
                preparation_clock = self.core.context.clock()
                if self._stop.is_set():
                    return
                self._collection_current(before, started, collection_end, bootstrap=bootstrap)
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
                self._collection_current(before, started, collection_end, bootstrap=bootstrap)
        if after is None:
            raise AccessDenied("Bounded own-unit dependency recollection was exhausted")
        if after["state"] != "ready" or after["graph_version"] != before["graph_version"]:
            raise AccessDenied("Changed dependency facts require guarded reconciliation")
        # Observe can race an expiring prior graph while waiting for SQLite.
        # The existing Core check detects an invalidated original attempt; this
        # read-only boundary grants no command, renewal or publication.
        current, attempt = self._facts()
        if successor:
            self.core.scheduling.successor_delivery(self.handle, current.id, attempt.id)
        elif self._cold_ref is not None:
            self.core.scheduling.cold_delivery(
                self.handle, current.id, attempt.id, self._cold_ref
            )
            self._cold_bound = True
        with self.core.execution.worker_commit_boundary(
            self.handle,
            current.id,
            attempt.id,
            current.version,
            attempt.version,
            current.decision_revision,
        ):
            pass
        now = self.core.context.clock()
        local_times = (time.monotonic() - preparation_started, now - preparation_clock)
        if not all(elapsed >= 0 for elapsed in local_times):
            raise AccessDenied("Local renewal preparation clock moved backwards")
        self._preparation_latency = max(self._preparation_latency, *local_times)
        self._next_refresh_at = refresh_schedule(
            after["expires_at"], self.deadline, now, self._preparation_latency,
        )

    def _collection_current(self, before, started, end, *, bootstrap=False):
        current, _ = self._facts()
        if current.observed_status != "In Progress" or current.sync_state != "confirmed":
            raise AccessDenied("Task synchronization changed during dependency collection")
        if time.monotonic() - started > MAX_COLLECTION_SECONDS or self.core.context.clock() >= end:
            raise AccessDenied("Dependency collection outlived its original current authority")
        latest = self.core.scheduling.explain(self.handle, current.id)
        if (latest["state"] != "ready" and not bootstrap) or latest["graph_version"] != before[
            "graph_version"
        ]:
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
        self.prepare_local()
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
        self.prepare_local()
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
        while not self._stop.wait(CHECK_INTERVAL_SECONDS):
            try:
                self.prepare_local(cancelled=self._stop)
                self.check()
                if self.core.context.clock() >= self._next_refresh_at:
                    self._refresh()
            except Exception as error:
                if not (
                    isinstance(error, DependencyPublicationCancelled)
                    and error.cancelled is self._stop
                    and self._stop.is_set()
                ):
                    self._error = error
                self._stop.set()
                return

    def prepare_local(self, *, cancelled=None):
        if self._assignment is None or self._mode != "observing":
            return
        if self._error is not None:
            raise AccessDenied("Delivery dependency observation failed") from self._error
        task, _ = self._facts()
        prepare = getattr(self.source, "prepare_current_milestones", None)
        if not callable(prepare):
            return
        current = self.core.scheduling.explain(self.handle, task.id)
        boundary = getattr(self.source, "publication_boundary", None)
        with (
            boundary(task.id, not_after=min(self.deadline, current["expires_at"] or self.deadline),
                     cancelled=cancelled)
            if callable(boundary) else nullcontext()
        ):
            self._facts()
            prepare(None, task_id=task.id)
        self._facts()

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
