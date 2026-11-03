"""Trusted workflow configuration; caller requests cannot provide commands or verdicts."""

import math
from dataclasses import asdict, dataclass

from ai_task_manager.domains.checks.models import fingerprint, identifier, repo_path
from ai_task_manager.domains.work.errors import ValidationError


@dataclass(frozen=True)
class WorkflowPlan:
    id: str
    command_ref: str
    host: str
    base: str
    allowed_files: tuple[str, ...]
    artifacts: tuple[str, ...]
    deadline: float
    max_steps: int = 16
    commit_checks: tuple[str, ...] = ()

    def __post_init__(self):
        for value in (self.id, self.command_ref, self.host, self.base):
            identifier(value)
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 128:
            raise ValidationError("A finite trusted workflow step bound is required")
        if type(self.deadline) not in {int, float} or not math.isfinite(self.deadline):
            raise ValidationError("An original finite deadline is required")
        for files in (self.allowed_files, self.artifacts):
            if type(files) is not tuple or not files or len(set(files)) != len(files):
                raise ValidationError("Explicit unique file requirements are required")
            for name in files:
                identifier(name)
                repo_path(name)
        if type(self.commit_checks) is not tuple or len(set(self.commit_checks)) != len(
            self.commit_checks
        ):
            raise ValidationError("Distinct installed commit check references are required")
        for reference in self.commit_checks:
            identifier(reference)
        if not set(self.artifacts) <= set(self.allowed_files):
            raise ValidationError("Artifacts must belong to the trusted allowed file set")

    @property
    def digest(self):
        return fingerprint(asdict(self))


class WorkflowPlans:
    """Installed trusted composition only; run/resume never accepts plan JSON."""

    def __init__(self, plans):
        if type(plans) is not tuple or any(type(p) is not WorkflowPlan for p in plans):
            raise ValidationError("Typed trusted workflow plans are required")
        self._plans = {p.id: p for p in plans}
        if len(self._plans) != len(plans):
            raise ValidationError("Plan identities must be unique")

    def get(self, reference):
        if reference not in self._plans:
            raise ValidationError("Workflow plan is not configured")
        return self._plans[reference]
