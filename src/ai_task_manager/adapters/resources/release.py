"""Resource release needs project policy independently of observed process exit."""

from dataclasses import dataclass
from typing import Protocol

from ai_task_manager.supervisor.models import RuntimeRefused

from .release_git import release_git


class ReleasePolicy(Protocol):
    def authorize(self, core, handle, attempt, resources): ...


class DeliveryReleasePolicy:
    def __init__(self, repository, integration_ref="HEAD"):
        self.repository = repository
        self.integration_ref = integration_ref

    def authorize(self, core, handle, attempt, resources):
        task = core.get_task(handle, attempt.task_id)
        if task.sync_state != "confirmed":
            raise RuntimeRefused("Current resource ownership is foreign or unconfirmed")
        if task.code_bearing and not (
            task.delivered_once
            or (task.observed_status == "Canceled" and task.sync_state == "confirmed")
        ):
            raise RuntimeRefused("Code resources need trusted delivery or explicit cancellation")
        # Cancellation never grants permission to discard unmerged committed work.
        try:
            release_git(
                self.repository,
                "merge-base",
                "--is-ancestor",
                resources["worktree"]["head"],
                self.integration_ref,
            )
        except RuntimeRefused as error:
            raise RuntimeRefused("Unmerged resource commits require preservation") from error


@dataclass(frozen=True)
class RemovalReceipt:
    attempt_id: str
    kind: str
    resource_id: str
    source_ref: str


class RemovalVerifier(Protocol):
    def verify_removal(self, opaque_ref: str) -> RemovalReceipt: ...


class RejectRemoval:
    def verify_removal(self, opaque_ref):
        raise RuntimeRefused("Trusted project removal evidence is unavailable")
