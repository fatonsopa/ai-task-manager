"""Compose Core guards with runtime facts; never write task policy tables."""

import math
from dataclasses import asdict, dataclass

from .models import RuntimeRefused


@dataclass(frozen=True)
class Dispatch:
    attempt_id: str
    resources: object
    worker: object


class WorkerSupervisor:
    def __init__(self, core, journal, resources, runtime, proofs, provider, accounting):
        self.core, self.journal, self.resources = core, journal, resources
        self.runtime, self.proofs, self.provider = runtime, proofs, provider
        self.accounting = accounting

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
        deadline,
        base="HEAD",
        source_readbacks=None,
    ):
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or deadline <= self.journal.clock()
        ):
            raise RuntimeRefused("A future finite deadline is required before reservation")
        reserve_source = (
            source_readbacks(
                phase="reserve",
                task_id=task_id,
                expected_version=expected_version,
                decision_revision=decision_revision,
                attempt_id=None,
                operation_key=f"{operation_key}:reserve",
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
            f"{operation_key}:reserve",
            **({"readback_ref": reserve_source} if source_readbacks is not None else {}),
        )
        self.core.execution.require_reserved_resources(handle, attempt.id)
        _, scope = self.core.session_binding(handle)
        self.journal.claim(attempt, asdict(scope), deadline=deadline)
        worker = None
        try:
            pair = self.resources.prepare(attempt.id, base=base)
            worker = self.runtime.launch_waiting(attempt.id)
            task = self.core.get_task(handle, task_id)
            startup = self.proofs.startup(scope, attempt.id, worker.observation_id)
            start_source = (
                source_readbacks(
                    phase="start",
                    task_id=task_id,
                    expected_version=task.version,
                    decision_revision=task.decision_revision,
                    attempt_id=attempt.id,
                    operation_key=f"{operation_key}:start",
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
                f"{operation_key}:start",
                **({"readback_ref": start_source} if source_readbacks is not None else {}),
            )
            return Dispatch(attempt.id, pair, worker)
        except BaseException as error:
            self._abort(handle, attempt.id, worker, type(error).__name__, operation_key)
            raise

    def admit(self, handle, dispatch, *, operation_key):
        attempt = self.core.get_attempt(handle, dispatch.attempt_id)
        self.provider.confirm_pending(handle, attempt.task_id)
        self.resources.verify(dispatch.resources)
        task = self.core.get_task(handle, attempt.task_id)
        _, scope = self.core.session_binding(handle)
        proof = self.proofs.resources(scope, attempt.id, dispatch.resources.observation_id)
        return self.core.execution.running(
            handle, attempt.id, task.version, task.decision_revision, proof, operation_key
        )

    def check_boundary(self, handle, dispatch, command, attempt_version):
        owner = self.journal.attempt(dispatch.attempt_id)
        if self.journal.clock() >= owner["deadline"]:
            raise RuntimeRefused("Original attempt deadline has expired")
        self.accounting.check(command)
        if command.cwd.resolve() != dispatch.resources.worktree:
            raise RuntimeRefused("Assigned work must run in its owned worktree")
        self.resources.verify(dispatch.resources)
        return self.core.execution.validate_work_lease(handle, dispatch.attempt_id, attempt_version)

    def execute(self, handle, dispatch, command, *, operation_key):
        attempt = self.core.get_attempt(handle, dispatch.attempt_id)

        def boundary():
            return self.check_boundary(handle, dispatch, command, attempt.version)

        try:
            result = self.runtime.execute(dispatch.worker, command, boundary)
        except BaseException as error:
            result = self.runtime.stop(dispatch.worker, reason=type(error).__name__, state="failed")
            self._settle(handle, dispatch.attempt_id, result.observation_id, operation_key)
            raise
        self._settle(handle, dispatch.attempt_id, result.observation_id, operation_key)
        return result

    def stop(self, handle, dispatch, *, operation_key, reason="requested stop"):
        result = self.runtime.stop(dispatch.worker, reason=reason)
        self._settle(handle, dispatch.attempt_id, result.observation_id, operation_key)
        return result

    def _settle(self, handle, attempt_id, observation, operation_key):
        attempt = self.core.get_attempt(handle, attempt_id)
        task = self.core.get_task(handle, attempt.task_id)
        _, scope = self.core.session_binding(handle)
        ref = self.proofs.outcome(scope, attempt_id, observation)
        return self.core.execution.settle(
            handle, attempt_id, task.version, task.decision_revision, ref, operation_key
        )

    def _abort(self, handle, attempt_id, worker, reason, key):
        attempt = self.core.get_attempt(handle, attempt_id)
        if worker is not None:
            result = self.runtime.stop(worker, reason=reason, state="failed")
            fact = self.journal.observation(result.observation_id)
            quiescent = result.quiescent
        else:
            failures = self.journal.observations(attempt_id, "startup_failure")
            quiescent = (
                failures[-1]["confirmed_stop"]
                if failures
                else self.journal.effect(attempt_id, "launch") is None
            )
            fact = {"reason": reason}
        observation = self.journal.observe(
            attempt_id,
            "runtime_outcome",
            {
                "state": "failed" if quiescent else "unknown",
                "quiescent": quiescent,
                "worker_id": attempt.worker_id,
                "failure": reason,
                "source": fact,
            },
        )
        try:
            self._settle(handle, attempt_id, observation, f"{key}:abort")
        except Exception as error:
            self.journal.observe(attempt_id, "settlement_pending", {"reason": type(error).__name__})
