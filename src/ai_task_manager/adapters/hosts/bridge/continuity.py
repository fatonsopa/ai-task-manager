"""Fresh continuity of one authenticated process; never a new login or grant."""

from dataclasses import asdict

from ai_task_manager.adapters.hosts.startup import HostStartup
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .startup_gate import PreparedStartup


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
        self.expires_at = min(self.deadline, self.original["expires_at"])
        self.closed = False

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
        expires = min(self.deadline, n.journal.clock() + 30)
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
            },
            expires_at=expires,
        )
        return prepared, reference

    def acknowledge(self, prepared):
        self.check(prepared)
        self.expires_at = prepared.observed.expires_at

    def check(self, prepared):
        n = self.native
        if (
            self.closed
            or n.journal.clock() >= self.expires_at
            or not self._owner_current()
            or prepared.binding != self._bound[1]
            or prepared.observed.expires_at > self.deadline
        ):
            raise AccessDenied("Native continuity no longer has its original authority")
        return prepared.check(
            prepared.observed, {n.binding.attempt_id: n}, n.process_registry, n.journal.clock
        )

    def close(self):
        self.closed = True
