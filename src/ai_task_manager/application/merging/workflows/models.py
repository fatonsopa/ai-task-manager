"""Installed code delivery intent; public inputs select only an assigned reference."""

import math
from dataclasses import asdict, dataclass

from ai_task_manager.config.files import digest
from ai_task_manager.domains.merging.candidates import CandidateGrant
from ai_task_manager.domains.merging.contracts import token
from ai_task_manager.domains.work.errors import ValidationError


@dataclass(frozen=True)
class MergePlan:
    id: str
    grant: CandidateGrant
    deadline: float
    require_human_acceptance: bool = False

    def __post_init__(self):
        token(self.id)
        if type(self.grant) is not CandidateGrant:
            raise ValidationError("An installed completed-worker grant is required")
        self.grant.validate()
        if not math.isfinite(self.deadline) or type(self.require_human_acceptance) is not bool:
            raise ValidationError("Finite delivery bounds and acceptance policy are required")

    @property
    def digest(self):
        return digest(asdict(self))
