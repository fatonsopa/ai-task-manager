"""Resource release needs project policy independently of observed process exit."""

import subprocess
from dataclasses import dataclass
from typing import Protocol

from ai_task_manager.supervisor.models import RuntimeRefused


class ReleasePolicy(Protocol):
    def authorize(self, core, handle, attempt, resources): ...


class DeliveryReleasePolicy:
    def __init__(self, repository, integration_ref="HEAD"):
        self.repository = repository
        self.integration_ref = integration_ref

    def authorize(self, core, handle, attempt, resources):
        task = core.get_task(handle, attempt.task_id)
        if task.code_bearing and not (
            task.delivered_once
            or (task.observed_status == "Canceled" and task.sync_state == "confirmed")
        ):
            raise RuntimeRefused("Code resources need trusted delivery or explicit cancellation")
        # Cancellation never grants permission to discard unmerged committed work.
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "merge-base",
                "--is-ancestor",
                resources["worktree"]["head"],
                self.integration_ref,
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeRefused("Unmerged resource commits require preservation")


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
