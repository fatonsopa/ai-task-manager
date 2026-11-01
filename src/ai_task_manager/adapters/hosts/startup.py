"""Observed authenticated CLI startup is separate from the waiting harness."""

from dataclasses import dataclass
from typing import Protocol

from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Scope


@dataclass(frozen=True)
class HostStartup:
    scope: Scope
    attempt_id: str
    worker_id: str
    host: str
    process_id: int
    process_start_identity: str
    session_id: str
    principal_ref: str
    configuration_digest: str
    observation_ref: str
    expires_at: float


class HostStartupVerifier(Protocol):
    def verify_host_ready(self, opaque_ref: str) -> HostStartup: ...


class RejectHostStartup:
    def verify_host_ready(self, opaque_ref):
        raise EvidenceRequired("An observed authenticated host startup bridge is required")
