"""Trusted composition ports; public clients cannot implement or issue them."""

from typing import Protocol

from ai_task_manager.domains.work.models import Attempt


class WorkerOperations(Protocol):
    """A host bridge owns reviewed deadlines/plans and actual host admission."""

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
    ) -> Attempt: ...

    def execute_reviewed(self, handle, attempt_id, command_ref, operation_key): ...


class ProviderOperations(Protocol):
    def confirm_pending(self, handle, task_id): ...

    def run_once(self, handle): ...
