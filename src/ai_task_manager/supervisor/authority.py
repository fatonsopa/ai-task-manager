"""Trusted refresh orchestration; collection is separate from Core publication."""

import threading
from dataclasses import dataclass
from time import monotonic
from time import monotonic as diagnostic_clock
from uuid import uuid4

from ai_task_manager.domains.scheduling.models import WorkRenewal
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.observability.failures import FailureStage, capture_failure

MAX_CLOSE_SECONDS = 22.0
# This is an acceptance limit, not a claim that provider I/O is preempted.
MAX_COLLECTION_SECONDS = 20.0
CHECK_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class _Prepared:
    value: WorkRenewal
    owner: object
    native: object


class WorkAuthorities:
    """Installer-owned resolver shared by the workers of one enrolled store."""

    def __init__(self, core, dependencies):
        if dependencies.core is not core:
            raise AccessDenied("Dependency collection belongs to another Core")
        self.core, self.dependencies = core, dependencies
        self._bound = (core, dependencies, core.context.db, core.scheduling.sources)
        self._database_path = core.context.db.path
        with core.context.db.transaction() as conn:
            self.store_identity = core.context.db.identity(conn)
        self._prepared = {}

    def check(self):
        if (
            any(
                a is not b
                for a, b in zip(
                    self._bound,
                    (
                        self.core,
                        self.dependencies,
                        self.core.context.db,
                        self.core.scheduling.sources,
                    ),
                    strict=True,
                )
            )
            or self.core.context.db.path != self._database_path
            or self.core.scheduling.renewals is not self
        ):
            raise AccessDenied("The original authority source or store changed")

    def attach(self, handle, attempt, native, continuity, *, on_failure=None):
        self.check()
        if self.core.session_binding(handle) != self.dependencies.binding:
            raise AccessDenied("The dependency source belongs to another enrolled session")
        return AttemptAuthority(self, handle, attempt, native, continuity, on_failure)

    def refresh_for_start(
        self, handle, task_id, operation_key, *, expected_version, decision_revision
    ):
        self.check()
        if self.core.session_binding(handle) != self.dependencies.binding:
            raise AccessDenied("The dependency source belongs to another enrolled session")
        result = self.dependencies.refresh_for_start(
            task_id,
            operation_key,
            expected_version=expected_version,
            decision_revision=decision_revision,
        )
        self.check()
        return result

    def starting(self, handle, attempt, runtime, deadline):
        from .startup_authority import StartupAuthority

        self.check()
        return StartupAuthority(self, handle, attempt, runtime, deadline)

    def verify_renewal(self, reference):
        self.check()
        prepared = self._prepared.get(reference)
        if prepared is None:
            raise AccessDenied("Unobserved renewal cannot authorize work")
        prepared.owner.check_prepared(prepared.native)
        return prepared.value


class AttemptAuthority:
    def __init__(self, registry, handle, attempt, native, continuity, on_failure=None):
        self.registry, self.handle, self.attempt_id = registry, handle, attempt.id
        self.native, self.continuity = native, continuity
        self.task_id = attempt.task_id
        self.deadline = native.binding.deadline
        self._bound = (registry, handle, native, continuity, native.binding)
        self._collecting = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._failure = None
        self._retention_error = None
        self._current = None
        self._expires_at = None
        self._next_refresh_at = None
        self._preparation_latency = 0.0
        self._on_failure = on_failure
        self.failure_diagnostics = []
        self.diagnostic_retention_failed = False
        self.diagnostics_truncated = False
        self.collection_close_observations = []

    def _diagnose(self, error, stage, started=None):
        if len(self.failure_diagnostics) >= 8:
            self.diagnostics_truncated = True
            return
        report = capture_failure(error, stage, started=started)
        retained = {"failure": report, "retained": False}
        self.failure_diagnostics.append(retained)
        try:
            retained["reference"] = self.native.journal._append(
                "trace",
                {
                    "effect": "work_authority_failure_cause",
                    "attempt_id": self.attempt_id,
                    "failure": report,
                },
            )
            retained["retained"] = True
        except BaseException as retention_error:
            self.diagnostic_retention_failed = True
            retained["retention_failure"] = capture_failure(
                retention_error, FailureStage.DIAGNOSTIC_RETENTION
            )

    def _check_owner(self):
        self.registry.check()
        if (
            self._stop.is_set()
            or self._failure is not None
            or self.registry is not self._bound[0]
            or self.handle != self._bound[1]
            or self.native is not self._bound[2]
            or self.continuity is not self._bound[3]
            or self.native.binding is not self._bound[4]
            or self.registry.core.context.clock() >= self.deadline
            or self.deadline != self._bound[-1].deadline
        ):
            raise AccessDenied("Execution freshness is stopped, fenced or expired")
        if self._expires_at is not None and self.registry.core.context.clock() >= self._expires_at:
            raise AccessDenied("Expired work authority cannot be revived")

    def check_prepared(self, prepared=None):
        self._check_owner()
        value = prepared if prepared is not None else self._current
        if value is None:
            raise AccessDenied("Execution requires a prepared current observation")
        return self.continuity.check(value)

    def refresh(self):
        started = diagnostic_clock()
        if not self._collecting.acquire(blocking=False):
            return self.check_prepared()
        publication_attempted = publication_confirmed = False
        try:
            self._check_owner()
            core = self.registry.core
            if core.context.clock() >= self.deadline:
                raise AccessDenied("The original execution deadline cannot be extended")
            task = core.get_task(self.handle, self.task_id)
            collection_started = diagnostic_clock()
            collection_clock = core.context.clock()
            key = "work-freshness:" + str(uuid4())
            preparation = self.registry.dependencies.prepare(
                task.id,
                key,
                expected_version=task.version,
                decision_revision=task.decision_revision,
            )
            collection_times = (
                diagnostic_clock() - collection_started,
                core.context.clock() - collection_clock,
            )
            collection_elapsed = max(collection_times)
            if not all(0 <= elapsed <= MAX_COLLECTION_SECONDS for elapsed in collection_times):
                raise AccessDenied("Dependency collection exceeded its renewal acceptance window")
            # Closure can race with provider I/O. Its result is retained, but
            # closing never authorizes publishing a new work observation.
            self._check_owner()
            preparation_started = diagnostic_clock()
            preparation_clock = core.context.clock()
            # Prepare the current same-native witness after provider collection;
            # its unchanged 30s lifetime must not be spent waiting for the read.
            native, continuity_ref = self.continuity.prepare()
            self._check_owner()
            # The resolver is local and immutable. Provider I/O ended in prepare().
            snapshot = core.scheduling.sources.verify_snapshot(preparation.snapshot_ref)
            attempt = core.get_attempt(self.handle, self.attempt_id)
            sid, scope = core.session_binding(self.handle)
            if (
                attempt.task_id != self.task_id
                or attempt.id != self.native.binding.attempt_id
                or attempt.host != self.native.binding.host
                or self.native.binding.core_session != sid
                or self.native.binding.scope != scope
                or (
                    attempt.worker_id is not None
                    and attempt.worker_id != self.native.binding.worker_id
                )
            ):
                raise AccessDenied("The observed native worker is not the current Core owner")
            now = core.context.clock()
            value = WorkRenewal(
                scope,
                self.registry.store_identity,
                sid,
                task.id,
                task.version,
                task.decision_revision,
                attempt.id,
                attempt.version,
                attempt.worker_id,
                attempt.host,
                self.native.binding.configuration_digest,
                self.deadline,
                now,
                min(snapshot.expires_at, native.observed.expires_at, self.deadline),
                snapshot,
            )
            self._schedule_next_refresh(value, preparation_started, preparation_clock)
            reference = "work-renewal:" + str(uuid4())
            self.registry._prepared[reference] = _Prepared(value, self, native)
            self.check_prepared(native)
            publication_attempted = True
            view = core.scheduling.renew(self.handle, reference, key + ":commit")
            publication_confirmed = True
            self.continuity.acknowledge(native)
            self._current = native
            self._expires_at = value.expires_at
            # Include the actual publication/acknowledgement cost. A late local
            # result cannot schedule an immediate loop or undo its Core event.
            self._next_refresh_at = self._schedule_next_refresh(
                value, preparation_started, preparation_clock
            )
            self.native.journal._append(
                "trace",
                {
                    "effect": "work_authority_refreshed",
                    "reference": reference,
                    "attempt_id": self.attempt_id,
                    "source_revision": snapshot.source_revision,
                    "expires_at": value.expires_at,
                    "execution_deadline": self.deadline,
                    "collection_seconds": collection_elapsed,
                    "collection_acceptance_seconds": MAX_COLLECTION_SECONDS,
                    "next_refresh_at": self._next_refresh_at,
                    "native_continuity_ref": continuity_ref,
                },
            )
            return view
        except BaseException as error:
            if self._stop.is_set() and self._failure is None:
                item = {
                    "failure": capture_failure(
                        error, FailureStage.AUTHORITY_REFRESH, started=started
                    ),
                    "retained": False,
                }
                if len(self.collection_close_observations) < 8:
                    self.collection_close_observations.append(item)
                    try:
                        item["reference"] = self.native.journal._append(
                            "trace",
                            {
                                "effect": "work_authority_collection_closed",
                                "attempt_id": self.attempt_id,
                                "observation": item["failure"],
                                "publication_attempted": publication_attempted,
                                "renewal_published": (
                                    True
                                    if publication_confirmed
                                    else None
                                    if publication_attempted
                                    else False
                                ),
                            },
                        )
                        item["retained"] = True
                    except BaseException as retention_error:
                        item["retention_failure"] = capture_failure(
                            retention_error, FailureStage.DIAGNOSTIC_RETENTION
                        )
                raise
            self._failure = type(error).__name__
            self._diagnose(error, FailureStage.AUTHORITY_REFRESH, started)
            raise
        finally:
            self._collecting.release()

    def _schedule_next_refresh(self, value, started, core_started):
        now = self.registry.core.context.clock()
        local_times = (diagnostic_clock() - started, now - core_started)
        if not all(elapsed >= 0 for elapsed in local_times):
            raise AccessDenied("Local renewal preparation clock moved backwards")
        self._preparation_latency = max(self._preparation_latency, *local_times)
        cap = min(self.deadline, self.continuity.execution_not_after)
        if value.expires_at >= cap:
            return cap
        next_at = value.expires_at - (
            MAX_COLLECTION_SECONDS + CHECK_INTERVAL_SECONDS + self._preparation_latency
        )
        if next_at <= now:
            raise AccessDenied("Renewal leaves no safe next collection window")
        return next_at

    def ensure_current(self):
        if self._current is None or self.registry.core.context.clock() >= self._next_refresh_at:
            self.refresh()
        return self.check_prepared()

    def start(self):
        if self._thread is not None:
            raise AccessDenied("The refresh supervisor was already started")
        self.ensure_current()
        guard = self.native.startup.model_guard
        model = guard.prepare_effect()  # Full preparation is outside Core/model locks.

        def run():
            while not self._stop.wait(
                min(
                    CHECK_INTERVAL_SECONDS,
                    max(0.001, self._next_refresh_at - self.registry.core.context.clock()),
                )
            ):
                started = diagnostic_clock()
                try:
                    if self.registry.core.context.clock() >= self._next_refresh_at:
                        self.refresh()  # Provider collection never holds the model guard.
                    if self._stop.is_set():
                        return
                    # This observer holds no Core transaction while waiting for
                    # an ordinary short model effect. Reentrant current checks
                    # then keep the normal model→Core order, without changing
                    # the nonblocking reverse observation used inside Core.
                    with guard.current_boundary(model):
                        self.check_prepared()
                        core = self.registry.core
                        attempt = core.get_attempt(self.handle, self.attempt_id)
                        core.execution.validate_work_lease(self.handle, attempt.id, attempt.version)
                except BaseException as error:
                    if self._stop.is_set() and self._failure is None:
                        return
                    if self._failure is None:
                        self._failure = type(error).__name__
                    self._diagnose(error, FailureStage.AUTHORITY_MONITOR, started)
                    # Retain Core ownership. The caller settles only after complete
                    # native/refresh/command stop; a model message cannot release it.
                    stop_error = None
                    try:
                        stopped = self.native.stop()
                    except BaseException as failed_stop:
                        self._diagnose(failed_stop, FailureStage.AUTHORITY_STOP)
                        stopped, stop_error = None, type(failed_stop).__name__
                    try:
                        self.native.journal._append(
                            "trace",
                            {
                                "effect": "work_authority_refresh_failed",
                                "attempt_id": self.attempt_id,
                                "reason": type(error).__name__,
                                "native_stop_confirmed": getattr(stopped, "confirmed", None)
                                is True,
                                "native_stop_error": stop_error,
                                "core_ownership": "retained until normal stop reconciliation",
                            },
                        )
                    except BaseException as retention_error:
                        self._retention_error = type(retention_error).__name__
                    try:
                        if self._on_failure is not None:
                            self._on_failure(self._failure)
                    except BaseException as settlement_error:
                        self._diagnose(settlement_error, FailureStage.AUTHORITY_CALLBACK)
                        self._retention_error = type(settlement_error).__name__
                    return

        self._thread = threading.Thread(target=run, name="managed-authority-refresh", daemon=True)
        self._thread.start()

    def request_close(self):
        """Fence new work immediately; joining/cleanup is a separate step."""
        self._stop.set()

    def close(self):
        self.request_close()
        # Wait for the actual collection owner, which may be a tool/controller
        # thread rather than the background monitor. No work authority is live.
        remaining = max(0, self.deadline - self.registry.core.context.clock())
        end = monotonic() + min(MAX_CLOSE_SECONDS, remaining)
        collected = self._collecting.acquire(timeout=max(0, end - monotonic()))
        if collected:
            self._collecting.release()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, end - monotonic()))
        self.continuity.close()
        return (
            collected
            and (thread is None or not thread.is_alive())
            and self._retention_error is None
        )
