"""Runtime observations do not authorize task transitions or prove delivery."""

from dataclasses import dataclass
from pathlib import Path


class RuntimeRefused(RuntimeError):
    pass


class RuntimeUncertain(RuntimeError):
    pass


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    timeout_seconds: float
    host: str = "fixture"


@dataclass(frozen=True)
class ResourcePair:
    attempt_id: str
    worktree: Path
    database: Path
    worktree_id: str
    database_id: str
    observation_id: str


@dataclass(frozen=True)
class RuntimeResult:
    attempt_id: str
    observation_id: str
    outcome: str
    quiescent: bool
    reason: str | None
