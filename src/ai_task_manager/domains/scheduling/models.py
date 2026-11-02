"""Typed observations resolved by a configured trusted local receipt source."""

from dataclasses import dataclass
from typing import Protocol

from ai_task_manager.domains.work.errors import EvidenceRequired, WorkError
from ai_task_manager.domains.work.models import Scope


class SchedulingBlocked(WorkError):
    """The task has no current dependency-based work authority."""


@dataclass(frozen=True)
class Dependency:
    predecessor: str
    condition: str
    unit: str | None = None
    evidence_refs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DependencySnapshot:
    scope: Scope
    task_id: str
    prose_digest: str
    canonical_issue_id: str | None
    source_id: str
    source_revision: int
    observed_at: float
    expires_at: float
    completeness: str
    priority: str
    dependencies: tuple[Dependency, ...]
    previous_graph_version: int = 0


@dataclass(frozen=True)
class Completion:
    predecessor: str
    condition: str
    availability: str
    kind: str | None = None
    required_units: tuple[str, ...] = ()
    completed_units: tuple[str, ...] = ()
    unit: str | None = None
    commit: str | None = None
    artifact: str | None = None


@dataclass(frozen=True)
class Blocker:
    code: str
    dependency: str | None
    release_condition: str


@dataclass(frozen=True)
class Readiness:
    state: str
    blockers: tuple[Blocker, ...]


class SchedulingSources(Protocol):
    def verify_snapshot(self, opaque_ref: str) -> DependencySnapshot:
        """Resolve an immutable local receipt without provider/process work."""
        ...


class RejectSchedulingSources:
    def verify_snapshot(self, opaque_ref):
        raise EvidenceRequired("A trusted complete dependency snapshot is unavailable")
