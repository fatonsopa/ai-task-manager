"""Managed host admission composes existing Core policy and observed runtime facts."""

import math
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from time import monotonic as diagnostic_clock

from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.adapters.resources.paths import unchanged
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS
from ai_task_manager.observability.failures import FailureStage, capture_failure

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
        dependencies=None,
        decision_preparation=None,
        reservation_recovery=None,
    ):
        if isinstance(deadline, bool) or not math.isfinite(deadline) or deadline <= journal.clock():
            raise AccessDenied("Managed workers require the original finite future deadline")
        self.core, self.journal, self.resources, self.runtime = core, journal, resources, runtime
        self.proofs, self.provider, self.hosts = proofs, provider, host_factory
        self.deadline, self.principal_ref, self.base = deadline, principal_ref, base
        self.executions = executions or RejectExecutions()
        self.dispatches = {}
        self._waiting_stops = {}
        self.authorities = None
        self._freshness = {}
        self._startup_freshness = {}
        self.failure_diagnostics = []
        self.diagnostics_truncated = False
        self.decision_preparation = self._decision_preparation = decision_preparation
        self.reservation_recovery = reservation_recovery
        if dependencies is not None:
            self.bind_dependencies(dependencies)

    def bind_dependencies(self, source):
        """Trusted installer/service composition, never a model tool argument."""
        from ai_task_manager.supervisor.authority import WorkAuthorities

        if self.authorities is not None:
            if self.authorities.dependencies is not source:
                raise AccessDenied("The managed dependency source cannot be replaced")
            return
        if self.dispatches or self._freshness:
            raise AccessDenied("Dependency authority must be installed before dispatch")
        self.authorities = WorkAuthorities(self.core, source)
        self.core.scheduling.renewals = self.authorities

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
        self._prepare_decision(decision_ref, handle, task_id, expected_version, decision_revision)
        if self.authorities is not None:
            self.authorities.refresh_for_start(
                handle,
                task_id,
                operation_key,
                expected_version=expected_version,
                decision_revision=decision_revision,
            )
        if self.journal.clock() >= self.deadline:
            raise AccessDenied("Original managed deadline expired during start preparation")
        import os

        from ai_task_manager.adapters.workflow_control.identity import process_start

        sid, scope = self.core.session_binding(handle)
        controller = {"pid": os.getpid(), "start": process_start(os.getpid())}
        reservation = {
            "task_id": task_id,
            "host": host,
            "scope": asdict(scope),
            "version": expected_version,
            "revision": decision_revision,
            "decision_ref": decision_ref,
            "reservation_key": operation_key + ":reserve",
            "controller": controller,
            "deadline": self.deadline,
        }
        identity = "managed-reservation:" + operation_key
        original = self.hosts.journal.intent(identity)
        if original is not None and original != reservation and self.reservation_recovery:
            from ai_task_manager.config.files import digest

            self.reservation_recovery(original, reservation)
            self.hosts.journal.intend(
                "managed-reservation-retry:" + digest({"identity": identity, "owner": controller}),
                {"original": identity, **reservation},
            )
        else:
            self.hosts.journal.intend(identity, reservation)
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
        self.core.execution.require_reserved_resources(handle, attempt.id)
        sid, scope = self.core.session_binding(handle)
        # Failure here means an existing intent, not permission to abort its other owner.
        owner = self.journal.claim(attempt, asdict(scope), deadline=self.deadline)
        self.hosts.journal.intend(
            "managed-owner:" + attempt.id,
            {
                "attempt_id": attempt.id,
                "worker_id": owner["worker_id"],
                "scope": asdict(scope),
                "controller": controller,
                "components": ["harness", "native", "commands", "collectors", "merge"],
            },
        )
        waiting = native = pair = None
        try:
            if self.authorities is not None:
                starting = self.authorities.starting(handle, attempt, self.journal, self.deadline)
                self._startup_freshness[attempt.id] = starting
                starting.start()
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
            start_identity = getattr(native, "start_identity_observations", None)
            identity = start_identity() if callable(start_identity) else None
            if self.authorities is not None:
                self._startup_freshness[attempt.id].finish()
                from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity

                freshness = self.authorities.attach(
                    handle,
                    attempt,
                    native,
                    NativeContinuity(
                        native, identities=identity.observations if identity is not None else None
                    ),
                    on_failure=lambda reason: self._authority_lost(handle, attempt.id, reason),
                )
                self._freshness[attempt.id] = freshness
                native.authority = freshness
                freshness.refresh()
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
            self._prepare_decision(
                decision_ref, handle, task_id, task.version, task.decision_revision
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
            if attempt.id in self._freshness:
                self._freshness[attempt.id].ensure_current()
            native.admit(handle, dispatch, running)
            if attempt.id in self._freshness:
                self._freshness[attempt.id].start()
            return running
        except BaseException as error:
            self._diagnose(attempt.id, error, FailureStage.MANAGED_START)
            self._abort(handle, attempt.id, waiting, native, operation_key, type(error).__name__)
            raise

    def _prepare_decision(self, ref, handle, task_id, version, revision):
        if self.decision_preparation is not self._decision_preparation:
            raise AccessDenied("The trusted decision preparation owner changed")
        if self.decision_preparation is not None:
            self.decision_preparation.prepare_consumption(
                ref,
                handle=handle,
                task_id=task_id,
                expected_version=version,
                decision_revision=revision,
            )

    def execute_worker_command(self, handle, attempt_id, command_ref, operation_key):
        attempt = self.core.get_attempt(handle, attempt_id)
        if self.executions.execution_kind(command_ref, attempt.task_id) not in {
            "confined",
            "merge",
        }:
            raise AccessDenied("A full assigned model invocation is not a worker command")
        return self.execute_reviewed(handle, attempt_id, command_ref, operation_key)

    def execute_reviewed(self, handle, attempt_id, command_ref, operation_key):
        started = diagnostic_clock()
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
            self._diagnose(attempt_id, error, FailureStage.MANAGED_EXECUTION, started)
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
                    self._diagnose(attempt_id, settlement_error, FailureStage.MANAGED_SETTLEMENT)
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

    def _diagnose(self, attempt_id, error, stage, started=None):
        if len(self.failure_diagnostics) >= 16:
            self.diagnostics_truncated = True
            return
        item = {"failure": capture_failure(error, stage, started=started), "retained": False}
        self.failure_diagnostics.append(item)
        try:
            item["reference"] = self.journal.observe(
                attempt_id, "managed_failure_cause", {"failure": item["failure"]}
            )
            item["retained"] = True
        except BaseException as retention_error:
            item["retention_failure"] = capture_failure(
                retention_error, FailureStage.DIAGNOSTIC_RETENTION
            )

    def _boundary(self, handle, dispatch, version):
        if self.journal.clock() >= self.deadline:
            raise AccessDenied("Original managed deadline expired")
        if dispatch.attempt_id in self._freshness:
            self._freshness[dispatch.attempt_id].ensure_current()
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
                if dispatch.attempt_id in self._freshness:
                    self._freshness[dispatch.attempt_id].check_prepared()
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
            self._diagnose(attempt_id, error, FailureStage.MANAGED_SETTLEMENT)
            self.journal.observe(attempt_id, "settlement_pending", {"reason": type(error).__name__})

    def _finish(self, handle, attempt_id, waiting, native, key, reason, state):
        outcomes = []
        collectors = []
        components = {}
        # End transport observations before fencing their work authority. This
        # cancels readers; it grants no terminal frame or new effect permission.
        cancel_reads = getattr(native, "begin_shutdown", None)
        if callable(cancel_reads):
            cancel_reads()
        if attempt_id in self._startup_freshness:
            collectors.append(("startup_collector", self._startup_freshness[attempt_id]))
        if attempt_id in self._freshness:
            collectors.append(("active_collector", self._freshness[attempt_id]))
        for name, collector in collectors:
            try:
                collector.request_close()
                components[name] = True
            except BaseException as error:
                self._diagnose(attempt_id, error, FailureStage.MANAGED_STOP)
                components[name] = False
        try:
            outcomes.append(self.executions.reconcile_stopped(attempt_id) is True)
        except BaseException as error:
            self._diagnose(attempt_id, error, FailureStage.MANAGED_STOP)
            outcomes.append(False)
        components["commands"] = outcomes[-1]
        if native is not None:
            try:
                stop_until = getattr(native, "stop_until", None)
                stopped = stop_until(self.deadline) if callable(stop_until) else native.stop()
                outcomes.append(stopped.confirmed)
            except BaseException as error:
                self._diagnose(attempt_id, error, FailureStage.MANAGED_STOP)
                outcomes.append(False)
            components["native"] = outcomes[-1]
        elif self.journal.effect(attempt_id, "native_launch") is not None:
            # launch() may have failed after creating a CLI. Only its trusted owner can reconcile.
            try:
                outcomes.append(self.hosts.reconcile_stopped(attempt_id))
            except BaseException as error:
                self._diagnose(attempt_id, error, FailureStage.MANAGED_STOP)
                outcomes.append(False)
            components["native"] = outcomes[-1]
        # Model/tool authority is already fenced and the native owner is stopped
        # promptly. Only then drain already-owned provider collection off-Core.
        collector_outcomes = []
        for name, collector in collectors:
            try:
                closed = collector.close()
            except BaseException as error:
                self._diagnose(attempt_id, error, FailureStage.MANAGED_STOP)
                closed = False
            components[name] = components[name] and closed
            collector_outcomes.append(components[name])
        outcomes = collector_outcomes + outcomes
        authority_failed = any(collector._failure is not None for _, collector in collectors)
        if state == "done" and authority_failed:
            state = "failed"
        if waiting is not None:
            try:
                stopped = self._waiting_stops.get(attempt_id)
                if stopped is None:
                    stopped = self.runtime.stop(waiting, reason=reason)
                    if stopped.quiescent:
                        self._waiting_stops[attempt_id] = stopped
                outcomes.append(stopped.quiescent)
            except BaseException as error:
                self._diagnose(attempt_id, error, FailureStage.MANAGED_STOP)
                outcomes.append(False)
            components["harness"] = outcomes[-1]
        elif self.journal.effect(attempt_id, "launch") is not None:
            outcomes.append(False)
            components["harness"] = False
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
                "stop_components": components,
                "authority_failed_before_close": authority_failed,
            },
        )
        return self._settle_observation(handle, attempt_id, observation, key)

    def _authority_lost(self, handle, attempt_id, reason):
        """Record uncertainty promptly; only complete later stop can release ownership."""
        attempt = self.core.get_attempt(handle, attempt_id)
        if attempt.state not in ACTIVE_ATTEMPTS or attempt.state == "unknown":
            return attempt
        observation = self.journal.observe(
            attempt_id,
            "runtime_outcome",
            {
                "state": "unknown",
                "quiescent": False,
                "worker_id": attempt.worker_id,
                "managed_stop": True,
                "reason": "authority_refresh_failed:" + reason,
            },
        )
        return self._settle_observation(
            handle, attempt_id, observation, "authority-loss:" + observation
        )

    def _settle_observation(self, handle, attempt_id, observation, key):
        attempt = self.core.get_attempt(handle, attempt_id)
        task = self.core.get_task(handle, attempt.task_id)
        _, scope = self.core.session_binding(handle)
        proof = self.proofs.outcome(scope, attempt_id, observation)
        return self.core.execution.settle(
            handle, attempt_id, task.version, task.decision_revision, proof, key
        )
