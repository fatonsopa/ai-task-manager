"""Managed host admission composes existing Core policy and observed runtime facts."""

import math
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.adapters.resources.paths import unchanged
from ai_task_manager.domains.work.errors import AccessDenied

from .models import RuntimeUncertain


@dataclass(frozen=True)
class ManagedDispatch:
    attempt_id: str
    resources: object
    waiting: object
    host: object


class RejectExecutions:
    def reconcile_stopped(self, attempt_id):
        return True

    def execute(self, **kwargs):
        raise AccessDenied("A reviewed accounted model/command execution registry is required")


class ManagedWorkers:
    def __init__(
        self,
        core,
        journal,
        resources,
        runtime,
        proofs,
        provider,
        host_factory,
        *,
        deadline,
        principal_ref,
        executions=None,
        base="HEAD",
    ):
        if isinstance(deadline, bool) or not math.isfinite(deadline) or deadline <= journal.clock():
            raise AccessDenied("Managed workers require the original finite future deadline")
        self.core, self.journal, self.resources, self.runtime = core, journal, resources, runtime
        self.proofs, self.provider, self.hosts = proofs, provider, host_factory
        self.deadline, self.principal_ref, self.base = deadline, principal_ref, base
        self.executions = executions or RejectExecutions()
        self.dispatches = {}
        self._waiting_stops = {}

    def start(
        self,
        handle,
        task_id,
        host,
        *,
        expected_version,
        decision_revision,
        decision_ref,
        operation_key,
    ):
        if host not in {"claude", "codex"} or self.journal.clock() >= self.deadline:
            raise AccessDenied("Unsupported host or original managed deadline expired")
        # The factory must positively verify installed restrictions and a current,
        # reviewed launch-accounting plan before any reservation or native effect.
        self.hosts.check_admission(host)
        attempt = self.core.execution.reserve(
            handle,
            task_id,
            host,
            expected_version,
            decision_revision,
            decision_ref,
            operation_key + ":reserve",
        )
        sid, scope = self.core.session_binding(handle)
        # Failure here means an existing intent, not permission to abort its other owner.
        owner = self.journal.claim(attempt, asdict(scope), deadline=self.deadline)
        waiting = native = pair = None
        try:
            pair = self.resources.prepare(attempt.id, base=self.base)
            waiting = self.runtime.launch_waiting(attempt.id)
            self.journal.intend(
                attempt.id,
                "native_launch",
                {"host": host, "worker_id": owner["worker_id"], "deadline": self.deadline},
            )
            binding = HostBinding(
                scope,
                sid,
                attempt.id,
                owner["worker_id"],
                host,
                self.principal_ref,
                self.hosts.configuration_digest(host, pair),
                self.deadline,
            )
            native = self.hosts.launch(binding, pair)
            # initialize() accepts no assigned task prompt and exposes no work lease.
            ready_ref = native.initialize()
            startup = self.proofs.startup(
                scope, attempt.id, waiting.observation_id, host_ready_ref=ready_ref
            )
            task = self.core.get_task(handle, task_id)
            self.core.execution.start(
                handle,
                attempt.id,
                task.version,
                task.decision_revision,
                startup,
                decision_ref,
                operation_key + ":start",
            )
            self.provider.confirm_pending(handle, task_id)
            native.verify_active()
            self.resources.verify(pair)
            task = self.core.get_task(handle, task_id)
            resource_ref = self.proofs.resources(scope, attempt.id, pair.observation_id)
            running = self.core.execution.running(
                handle,
                attempt.id,
                task.version,
                task.decision_revision,
                resource_ref,
                operation_key + ":running",
            )
            dispatch = ManagedDispatch(attempt.id, pair, waiting, native)
            self.dispatches[attempt.id] = dispatch
            native.admit(handle, dispatch, running)
            return running
        except BaseException as error:
            self._abort(handle, attempt.id, waiting, native, operation_key, type(error).__name__)
            raise

    def execute_reviewed(self, handle, attempt_id, command_ref, operation_key):
        dispatch = self.dispatches.get(attempt_id)
        if dispatch is None:
            raise RuntimeUncertain(
                "Owned managed dispatch is unavailable; reconcile before execution"
            )
        attempt = self.core.get_attempt(handle, attempt_id)
        self._boundary(handle, dispatch, attempt.version)
        return self.executions.execute(
            handle=handle,
            attempt=attempt,
            resources=dispatch.resources,
            host=dispatch.host,
            command_ref=command_ref,
            operation_key=operation_key,
            boundary=lambda: self._boundary(handle, dispatch, attempt.version),
            effect_boundary=lambda: self._short_effect(handle, dispatch, attempt.version),
            merge_authority=lambda: self._merge_authority(handle, dispatch, attempt.version),
        )

    def _boundary(self, handle, dispatch, version):
        if self.journal.clock() >= self.deadline:
            raise AccessDenied("Original managed deadline expired")
        dispatch.host.verify_active()
        self.resources.verify(dispatch.resources)
        return self.core.execution.validate_work_lease(handle, dispatch.attempt_id, version)

    def _merge_authority(self, handle, dispatch, version):
        if self.journal.clock() >= self.deadline:
            raise AccessDenied("Original managed deadline expired")
        # The existing foreground script may stop/drop its own application DB.
        # Task authority lives in the separate coordinator store and survives that.
        return self.core.execution.validate_work_lease(handle, dispatch.attempt_id, version)

    @contextmanager
    def _short_effect(self, handle, dispatch, version):
        dispatch.host.verify_active()
        fact = self.resources.verify(dispatch.resources)
        with self.core.execution.work_boundary(handle, dispatch.attempt_id, version):
            if self.journal.clock() >= self.deadline:
                raise AccessDenied("Original managed deadline expired")
            for kind, path in (
                ("worktree", dispatch.resources.worktree),
                ("database", dispatch.resources.database),
            ):
                unchanged(path, fact[kind]["physical"])
            yield

    def stop(self, handle, attempt_id, *, operation_key, reason="requested stop"):
        dispatch = self.dispatches.get(attempt_id)
        if dispatch is None:
            raise RuntimeUncertain("Unknown runtime retains capacity until trusted reconciliation")
        return self._finish(
            handle, attempt_id, dispatch.waiting, dispatch.host, operation_key, reason, "paused"
        )

    def _abort(self, handle, attempt_id, waiting, native, key, reason):
        try:
            self._finish(handle, attempt_id, waiting, native, key + ":abort", reason, "failed")
        except BaseException as error:
            self.journal.observe(attempt_id, "settlement_pending", {"reason": type(error).__name__})

    def _finish(self, handle, attempt_id, waiting, native, key, reason, state):
        outcomes = []
        try:
            outcomes.append(self.executions.reconcile_stopped(attempt_id) is True)
        except BaseException:
            outcomes.append(False)
        if native is not None:
            try:
                outcomes.append(native.stop().confirmed)
            except BaseException:
                outcomes.append(False)
        elif self.journal.effect(attempt_id, "native_launch") is not None:
            # launch() may have failed after creating a CLI. Only its trusted owner can reconcile.
            outcomes.append(self.hosts.reconcile_stopped(attempt_id))
        if waiting is not None:
            try:
                stopped = self._waiting_stops.get(attempt_id)
                if stopped is None:
                    stopped = self.runtime.stop(waiting, reason=reason)
                    if stopped.quiescent:
                        self._waiting_stops[attempt_id] = stopped
                outcomes.append(stopped.quiescent)
            except BaseException:
                outcomes.append(False)
        elif self.journal.effect(attempt_id, "launch") is not None:
            outcomes.append(False)
        quiescent = all(value is True for value in outcomes)
        observation = self.journal.observe(
            attempt_id,
            "runtime_outcome",
            {
                "state": state if quiescent else "unknown",
                "quiescent": quiescent,
                "worker_id": self.journal.attempt(attempt_id)["worker_id"],
                "reason": reason,
                "model_success": "not_established",
                "native_and_harness_stop": outcomes,
            },
        )
        attempt = self.core.get_attempt(handle, attempt_id)
        task = self.core.get_task(handle, attempt.task_id)
        _, scope = self.core.session_binding(handle)
        proof = self.proofs.outcome(scope, attempt_id, observation)
        return self.core.execution.settle(
            handle, attempt_id, task.version, task.decision_revision, proof, key
        )
