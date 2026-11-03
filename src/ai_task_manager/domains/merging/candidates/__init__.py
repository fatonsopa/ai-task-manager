"""Immutable candidate inputs/results; never publication or task-state authority."""

from dataclasses import asdict, dataclass

from ai_task_manager.config.files import digest
from ai_task_manager.domains.merging.contracts import Binding, oid, refname, token


@dataclass(frozen=True)
class CandidateGrant:
    binding: Binding
    source_ref: str
    source_head: str
    target_head: str
    task_version: int
    attempt_version: int
    decision_revision: int
    decision_ref: str
    owner_ref: str

    def validate(self):
        self.binding.validate()
        refname(self.source_ref)
        oid(self.source_head)
        oid(self.target_head)
        token(self.decision_ref)
        token(self.owner_ref)
        if any(
            type(v) is not int or v < 0
            for v in (self.task_version, self.attempt_version, self.decision_revision)
        ):
            raise ValueError("Exact nonnegative Core versions are required")
        return self

    @property
    def digest(self):
        self.validate()
        return digest(asdict(self))


@dataclass(frozen=True)
class CandidateResult:
    operation_ref: str
    status: str
    source: str
    target: str
    candidate_ref: str
    path: str
    commit: str | None
    tree: str | None
    reason: str | None = None
