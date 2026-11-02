"""Fresh continuity of one authenticated process; never a new login or grant."""

import math
from dataclasses import asdict, dataclass

from ai_task_manager.adapters.hosts.startup import HostStartup
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .invocations import InitializationInvocation
from .startup_gate import PreparedStartup


@dataclass(frozen=True)
class _ContinuityLimits:
    identity_not_after: float
    execution_not_after: float
    process_deadline: float

    @property
    def not_after(self):
        return min(self.identity_not_after, self.execution_not_after)


class NativeContinuity:
    def __init__(self, native):
        self.native = native
        self._bound = (
            native,
            native.binding,
            native.process,
            native.startup,
            native.policy,
            native.session,
        )
        native.verify_active()
        self.original = native.journal._read(native.startup.ready_ref, "startup")
        self._original_digest = digest(self.original)
        self.deadline = native.binding.deadline
        invocation = native.process.invocation
        if not isinstance(invocation, InitializationInvocation):
            raise AccessDenied("Continuity needs its original registered execution owner")
        values = (
            self.original["expires_at"],
            self.deadline,
            native.process.deadline,
            invocation.native.binding.deadline,
            invocation.relay.binding.deadline,
        )
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
            raise AccessDenied("Original identity and execution limits must be finite")
        self._limits = _ContinuityLimits(values[0], min(values[1:]), values[2])
        self._original_limits = self._limits
        self._cap_owner = (invocation, invocation.native.binding, invocation.relay.binding)
        self.expires_at = self._limits.not_after
        self.closed = False

    @property
    def identity_not_after(self):
        return self._original_limits.identity_not_after

    @property
    def execution_not_after(self):
        return self._original_limits.execution_not_after

    def _owner_current(self):
        n = self.native
        return (
            all(
                a is b
                for a, b in zip(
                    self._bound,
                    (n, n.binding, n.process, n.startup, n.policy, n.session),
                    strict=True,
                )
            )
            and self.deadline == self._bound[1].deadline
            and self._limits is self._original_limits
            and self.original["expires_at"] == self.identity_not_after
            and n.process.deadline == self._limits.process_deadline
            and n.process.invocation is self._cap_owner[0]
            and n.process.invocation is n.process._bound_invocation
            and n.process.invocation.native.binding is self._cap_owner[1]
            and n.process.invocation.relay.binding is self._cap_owner[2]
            and type(self.expires_at) in {int, float}
            and math.isfinite(self.expires_at)
            and self.expires_at <= self._limits.not_after
        )

    def prepare(self):
        n = self.native
        if (
            self.closed
            or n.journal.clock() >= self.expires_at
            or not self._owner_current()
            or digest(self.original) != self._original_digest
        ):
            raise AccessDenied("Expired, stopped or replaced native continuity cannot be renewed")
        # Complete policy/process/history preparation is outside Core/effect locks.
        identity = n.verify_active()
        data = self.original["payload"]
        if (identity.pid, identity.start_identity, n.startup.native_session) != (
            data["process_id"],
            data["process_start_identity"],
            data["session_id"],
        ):
            raise AccessDenied("The authenticated native session owner changed")
        expires = min(self._limits.not_after, n.journal.clock() + 30)
        observed = HostStartup(
            n.binding.scope,
            n.binding.attempt_id,
            n.binding.worker_id,
            n.binding.host,
            identity.pid,
            identity.start_identity,
            n.startup.native_session,
            n.binding.principal_ref,
            n.binding.configuration_digest,
            n.startup.ready_ref,
            expires,
        )
        metadata = n.policy.prepare_admission()
        n._flush_source_observations()
        prepared = PreparedStartup(
            observed,
            n,
            n.process,
            n.process.native_exit,
            n.startup,
            n.startup.model_guard,
            n.startup.configuration,
            n.policy,
            n.binding,
            n.session,
            metadata,
        )
        prepared.check(observed, {n.binding.attempt_id: n}, n.process_registry, n.journal.clock)
        n._flush_source_observations()
        reference = n.journal._append(
            "trace",
            {
                "effect": "native_continuity_observed",
                "observation": asdict(observed),
                "original_startup_digest": self._original_digest,
                "new_authentication": False,
                "execution_deadline": self.deadline,
                "identity_not_after": self.identity_not_after,
                "execution_not_after": self.execution_not_after,
            },
            expires_at=expires,
        )
        return prepared, reference

    def acknowledge(self, prepared):
        self.check(prepared)
        self.expires_at = min(prepared.observed.expires_at, self._limits.not_after)

    def check(self, prepared):
        n = self.native
        if (
            self.closed
            or n.journal.clock() >= self.expires_at
            or not self._owner_current()
            or prepared.binding != self._bound[1]
            or prepared.observed.expires_at > self._limits.not_after
        ):
            raise AccessDenied("Native continuity no longer has its original authority")
        return prepared.check(
            prepared.observed, {n.binding.attempt_id: n}, n.process_registry, n.journal.clock
        )

    def close(self):
        self.closed = True
