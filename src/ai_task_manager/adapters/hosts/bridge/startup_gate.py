"""Prepared startup ownership; consumption performs only local metadata/state checks."""

from dataclasses import asdict, dataclass

from ai_task_manager.domains.work.errors import AccessDenied


@dataclass(frozen=True)
class PreparedStartup:
    observed: object
    owner: object
    process: object
    native_exit: object
    observer: object
    guard: object
    configuration: object
    policy: object
    binding: object
    session: object
    policy_metadata: object
    identity: object = None

    def check(self, observed, owners, processes, clock):
        if (
            observed != self.observed
            or observed.expires_at <= clock()
            or owners.get(observed.attempt_id) is not self.owner
            or processes.get(observed.attempt_id) is not self.process
            or self.owner.process is not self.process
            or self.process.native_exit is not self.native_exit
            or self.owner.startup is not self.observer
            or self.owner.policy is not self.policy
            or self.owner.binding != self.binding
            or self.owner.session is not self.session
            or self.observer.binding != self.binding
            or self.observer.policy is not self.policy
            or self.observer.model_guard is not self.guard
            or self.observer.configuration is not self.configuration
            or self.guard.configuration is not self.configuration
            or self.observer.ready_ref != observed.observation_ref
            or self.observer.native_session != observed.session_id
            or self.process._closed
            or self.process.identity is None
            or (self.process.identity.pid, self.process.identity.start_identity)
            != (observed.process_id, observed.process_start_identity)
            or self.policy.configuration_digest != observed.configuration_digest
            or any(
                getattr(observed, name) != value
                for name, value in asdict(self.binding).items()
                if name
                in {"attempt_id", "worker_id", "host", "principal_ref", "configuration_digest"}
            )
            or self.binding.scope != observed.scope
        ):
            raise AccessDenied("Prepared startup no longer has its exact live owner")
        self.process.check_current_liveness()
        if self.identity is not None:
            if self.identity.owner.native is not self.owner:
                raise AccessDenied("Prepared identity belongs to another native owner")
            self.identity.check()
        with self.guard.startup_current_boundary():
            try:
                self.policy.check_admission_metadata(self.policy_metadata)
            except AccessDenied:
                # Reentrant under the same nonblocking observation. Never wait
                # for this lock again while Core is held, or write its journal.
                self.guard.configuration_changed(None, record=False)
                raise
        return self.observed
