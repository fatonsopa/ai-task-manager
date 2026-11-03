"""Typed captured judgments and prepared physical observations, never task policy."""

import math
from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.config.files import digest, strict_json
from ai_task_manager.domains.work.errors import EvidenceRequired

PREFIX = "workflow-review:"
KINDS = frozenset({"work_unit_complete", "non_code_completion"})


@dataclass(frozen=True)
class FileWitness:
    path: Path
    metadata: tuple | None
    identity_only: bool = False

    @staticmethod
    def observe(path, identity_only=False):
        path = Path(path).absolute()
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise EvidenceRequired("A reviewed source cannot use symlinks")
        if not path.exists():
            return None
        value = path.stat()
        if identity_only:
            return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink)
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    @classmethod
    def capture(cls, path, *, identity_only=False):
        path = Path(path).absolute()
        return cls(path, cls.observe(path, identity_only), identity_only)

    def check(self):
        if self.observe(self.path, self.identity_only) != self.metadata:
            raise EvidenceRequired("A prepared review source changed; prepare again outside Core")


@dataclass(frozen=True)
class Inspection:
    context_json: str
    witnesses: tuple[FileWitness, ...]

    @property
    def context(self):
        return strict_json(self.context_json)

    @property
    def digest(self):
        return digest(self.context)

    def check(self):
        for witness in self.witnesses:
            witness.check()


@dataclass(frozen=True)
class CapturedReview:
    reference: str
    context_digest: str
    reviewer_id: str
    host: str
    model: str | None
    provenance_ref: str
    trace_sha256: str
    source_digest: str
    criteria: tuple[tuple[str, str, str], ...]
    reason: str
    expires_at: float
    witnesses: tuple[FileWitness, ...]

    def accepted(self, expected, now):
        if (
            not math.isfinite(self.expires_at)
            or self.expires_at <= now
            or tuple(c[0] for c in self.criteria) != tuple(expected)
            or not self.reason.strip()
            or not self.reviewer_id
            or not self.provenance_ref
            or any(
                not reason.strip() or outcome not in {"accept", "reject"}
                for _, outcome, reason in self.criteria
            )
        ):
            raise EvidenceRequired("Captured review criteria, provenance or freshness failed")
        return all(outcome == "accept" for _, outcome, _ in self.criteria)
