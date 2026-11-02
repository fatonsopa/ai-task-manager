"""Trusted refresh orchestration; collection is separate from Core publication."""

import threading
from dataclasses import dataclass
from uuid import uuid4

from ai_task_manager.domains.scheduling.models import WorkRenewal
from ai_task_manager.domains.work.errors import AccessDenied


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
        self._on_failure = on_failure

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
        if not self._collecting.acquire(blocking=False):
            return self.check_prepared()
        try:
            self._check_owner()
            core = self.registry.core
            if core.context.clock() >= self.deadline:
                raise AccessDenied("The original execution deadline cannot be extended")
            task = core.get_task(self.handle, self.task_id)
            native, continuity_ref = self.continuity.prepare()
            key = "work-freshness:" + str(uuid4())
            preparation = self.registry.dependencies.prepare(
                task.id,
                key,
                expected_version=task.version,
                decision_revision=task.decision_revision,
            )
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
            reference = "work-renewal:" + str(uuid4())
            self.registry._prepared[reference] = _Prepared(value, self, native)
            self.check_prepared(native)
            view = core.scheduling.renew(self.handle, reference, key + ":commit")
            self.continuity.acknowledge(native)
            self._current = native
            self._expires_at = value.expires_at
            self.native.journal._append(
                "trace",
                {
                    "effect": "work_authority_refreshed",
                    "reference": reference,
                    "attempt_id": self.attempt_id,
                    "source_revision": snapshot.source_revision,
                    "expires_at": value.expires_at,
                    "execution_deadline": self.deadline,
                    "native_continuity_ref": continuity_ref,
                },
            )
            return view
        except BaseException as error:
            self._failure = type(error).__name__
            raise
        finally:
            self._collecting.release()

    def ensure_current(self):
        if self._current is None or self._expires_at - self.registry.core.context.clock() <= 10:
            self.refresh()
        return self.check_prepared()

    def start(self):
        if self._thread is not None:
            raise AccessDenied("The refresh supervisor was already started")
        self.ensure_current()

        def run():
            while not self._stop.wait(
                min(5, max(0.05, (self._expires_at - self.registry.core.context.clock()) / 3))
            ):
                try:
                    self.ensure_current()
                    core = self.registry.core
                    attempt = core.get_attempt(self.handle, self.attempt_id)
                    core.execution.validate_work_lease(self.handle, attempt.id, attempt.version)
                except BaseException as error:
                    # Retain Core ownership. The caller settles only after complete
                    # native/refresh/command stop; a model message cannot release it.
                    stop_error = None
                    try:
                        stopped = self.native.stop()
                    except BaseException as failed_stop:
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
                        self._retention_error = type(settlement_error).__name__
                    return

        self._thread = threading.Thread(target=run, name="managed-authority-refresh", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.continuity.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        return (thread is None or not thread.is_alive()) and self._retention_error is None
