"""Managed host admission composes existing Core policy and observed runtime facts."""

import math
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass

from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.adapters.resources.paths import unchanged
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS

from .models import RuntimeUncertain


@dataclass(frozen=True)
class ManagedDispatch:
    attempt_id: str
    resources: object
    waiting: object
    host: object


class RejectExecutions:
    def execution_kind(self, reference, task_id):
        raise AccessDenied("A reviewed execution registry is required")

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
        source_readbacks=None,
    ):
        if host not in {"claude", "codex"} or self.journal.clock() >= self.deadline:
            raise AccessDenied("Unsupported host or original managed deadline expired")
        # The factory must positively verify installed restrictions and a current,
        # reviewed launch-accounting plan before any reservation or native effect.
        self.hosts.check_admission(host)
        reserve_source = (
            source_readbacks(
                phase="reserve",
                task_id=task_id,
                expected_version=expected_version,
                decision_revision=decision_revision,
                attempt_id=None,
                operation_key=operation_key + ":reserve",
            )
            if source_readbacks is not None
            else None
        )
        attempt = self.core.execution.reserve(
            handle,
            task_id,
            host,
            expected_version,
            decision_revision,
            decision_ref,
            operation_key + ":reserve",
            **({"readback_ref": reserve_source} if source_readbacks is not None else {}),
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
            start_source = (
                source_readbacks(
                    phase="start",
                    task_id=task_id,
                    expected_version=task.version,
                    decision_revision=task.decision_revision,
                    attempt_id=attempt.id,
                    operation_key=operation_key + ":start",
                )
                if source_readbacks is not None
                else None
            )
            self.core.execution.start(
                handle,
                attempt.id,
                task.version,
                task.decision_revision,
                startup,
                decision_ref,
                operation_key + ":start",
                **({"readback_ref": start_source} if source_readbacks is not None else {}),
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

    def execute_worker_command(self, handle, attempt_id, command_ref, operation_key):
        attempt = self.core.get_attempt(handle, attempt_id)
        if self.executions.execution_kind(command_ref, attempt.task_id) not in {
            "confined",
            "merge",
        }:
            raise AccessDenied("A full assigned model invocation is not a worker command")
        return self.execute_reviewed(handle, attempt_id, command_ref, operation_key)

    def execute_reviewed(self, handle, attempt_id, command_ref, operation_key):
        dispatch = self.dispatches.get(attempt_id)
        if dispatch is None:
            raise RuntimeUncertain(
                "Owned managed dispatch is unavailable; reconcile before execution"
            )
        attempt = self.core.get_attempt(handle, attempt_id)
        kind = self.executions.execution_kind(command_ref, attempt.task_id)
        if attempt.state not in ACTIVE_ATTEMPTS:
            raise AccessDenied("The assigned worker is already settled")
        try:
            self._boundary(handle, dispatch, attempt.version)
            result = self.executions.execute(
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
        except BaseException as error:
            if kind == "model":
                try:
                    self._finish(
                        handle,
                        attempt_id,
                        dispatch.waiting,
                        dispatch.host,
                        operation_key + ":failed",
                        type(error).__name__,
                        "failed",
                    )
                except BaseException as settlement_error:
                    self.journal.observe(
                        attempt_id,
                        "settlement_pending",
                        {
                            "reason": type(settlement_error).__name__,
                            "invocation_error": type(error).__name__,
                        },
                    )
            raise
        if kind == "model":
            settled = self._finish(
                handle,
                attempt_id,
                dispatch.waiting,
                dispatch.host,
                operation_key + ":terminal",
                "assigned invocation ended",
                "done",
            )
            if settled.state == "unknown":
                raise RuntimeUncertain(
                    "Assigned model ended but complete owned stop is unconfirmed"
                )
        return result

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
        boundary = getattr(dispatch.host, "effect_boundary", None)
        if boundary is None:
            if self.core.get_attempt(handle, dispatch.attempt_id).host in {"claude", "codex"}:
                raise AccessDenied("A bound native model effect boundary is required")
            guard = nullcontext()
        else:
            guard = boundary()
        with guard:
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

    def reconcile(self, handle, attempt_id, *, operation_key):
        """Recover recorded complete stop or retain uncertainty without new effects."""
        attempt = self.core.get_attempt(handle, attempt_id)
        if attempt.state not in ACTIVE_ATTEMPTS:
            return attempt
        self.journal.attempt(attempt_id)
        outcomes = self.journal.observations(attempt_id, "runtime_outcome")
        managed = [row for row in outcomes if row.get("managed_stop") is True]
        if managed and managed[-1].get("quiescent") is True:
            return self._settle_observation(handle, attempt_id, managed[-1]["id"], operation_key)
        # A waiting-harness outcome cannot prove that separate host/container/merge
        # owners stopped. Lost waitable-child handles never authorize resurrection.
        observation = self.journal.observe(
            attempt_id,
            "runtime_outcome",
            {
                "state": "unknown",
                "quiescent": False,
                "worker_id": attempt.worker_id,
                "managed_stop": True,
                "reason": "complete_managed_stop_unavailable",
                "model_success": "not_established",
            },
        )
        return self._settle_observation(handle, attempt_id, observation, operation_key)

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
            try:
                outcomes.append(self.hosts.reconcile_stopped(attempt_id))
            except BaseException:
                outcomes.append(False)
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
        attempt = self.core.get_attempt(handle, attempt_id)
        observation = self.journal.observe(
            attempt_id,
            "runtime_outcome",
            {
                "state": state if quiescent else "unknown",
                "quiescent": quiescent,
                "worker_id": attempt.worker_id,
                "runtime_owner_worker_id": self.journal.attempt(attempt_id)["worker_id"],
                "managed_stop": True,
                "reason": reason,
                "model_success": "not_established",
                "native_and_harness_stop": outcomes,
            },
        )
        return self._settle_observation(handle, attempt_id, observation, key)

    def _settle_observation(self, handle, attempt_id, observation, key):
        attempt = self.core.get_attempt(handle, attempt_id)
        task = self.core.get_task(handle, attempt.task_id)
        _, scope = self.core.session_binding(handle)
        proof = self.proofs.outcome(scope, attempt_id, observation)
        return self.core.execution.settle(
            handle, attempt_id, task.version, task.decision_revision, proof, key
        )
