"""Trusted, task-bound declarations for the existing atomic admission owner."""

import math
from dataclasses import dataclass

from ai_task_manager.domains.checks.models import identifier, repo_path
from ai_task_manager.domains.work.errors import EvidenceRequired, ValidationError
from ai_task_manager.domains.work.models import Scope


@dataclass(frozen=True)
class AdmissionResources:
    scope: Scope
    task_id: str
    prose_digest: str
    decision_revision: int
    repository: str
    files: tuple[str, ...]
    locks: tuple[tuple[str, str], ...]
    expires_at: float | None = None
    witnesses: tuple[tuple[str, int, int, str], ...] = ()

    def validate(self, scope, task, now):
        if (type(self.scope) is not Scope or self.scope != scope
                or (self.task_id, self.prose_digest, self.decision_revision)
                != (task.id, task.prose_digest, task.decision_revision)):
            raise EvidenceRequired("Resource declaration belongs to another current task")
        if (type(self.expires_at) not in {int, float} or not math.isfinite(self.expires_at)
                or self.expires_at <= now):
            raise EvidenceRequired("Current finite resource declaration is required")
        identifier(self.repository)
        if (type(self.files) is not tuple or len(self.files) > 256
                or len(set(self.files)) != len(self.files)
                or type(self.locks) is not tuple or len(self.locks) > 256
                or len(set(self.locks)) != len(self.locks)):
            raise ValidationError("Explicit distinct bounded resource declarations are required")
        for name in self.files:
            identifier(name)
            repo_path(name)
        for pair in self.locks:
            if (type(pair) is not tuple or len(pair) != 2
                    or pair[0] not in {"migration", "test", "resource"}):
                raise ValidationError("A declared migration, test or resource lock is required")
            identifier(pair[1])
        return self


class MissingResourceDeclaration(EvidenceRequired):
    pass


class RejectResourceDeclarations:
    def read(self, scope, task):
        raise MissingResourceDeclaration("Current installed resource declarations are unavailable")


def file_overlap(left, right):
    """Literal file scopes also conflict with a file/directory path collision."""
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")
