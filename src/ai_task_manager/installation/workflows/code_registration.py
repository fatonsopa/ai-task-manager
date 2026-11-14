"""Installer-held code policy and existing Git/check capabilities."""

from dataclasses import asdict, dataclass

from ai_task_manager.adapters.git.profile import GitProfile
from ai_task_manager.application.merging.workflows import CandidateChecks
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.merging.contracts import refname, token
from ai_task_manager.domains.work.errors import AccessDenied

from .spec import BoundFile


@dataclass(frozen=True)
class CodeCapabilities:
    profile: GitProfile
    checks: CandidateChecks
    required_hooks: tuple[str, ...] = ()
    maximum_attempts: int = 1
    publication_worktree: str | None = None

    def require(self):
        if type(self.profile) is not GitProfile or type(self.checks) is not CandidateChecks:
            raise AccessDenied("Actual installed Git profile and candidate checks are required")
        if type(self.maximum_attempts) is not int or not 1 <= self.maximum_attempts <= 4:
            raise AccessDenied("Installed candidate attempts must be an integer from 1 to 4")
        if self.publication_worktree is not None:
            token(self.publication_worktree)
        if type(self.required_hooks) is not tuple:
            raise AccessDenied("Immutable installed commit guard selection required")


class CodeRegistration:
    def __init__(self, policy_file, prepare, *, sources=()):
        self.seal = BoundFile(policy_file)
        self.data = read_private(policy_file)
        if set(self.data) != {
            "reference",
            "scope",
            "task_id",
            "prose_digest",
            "target_ref",
            "require_human_acceptance",
        } or not callable(prepare):
            raise AccessDenied("Closed installed code policy and code-bound preparation required")
        token(self.data["reference"])
        refname(self.data["target_ref"])
        if type(self.data["require_human_acceptance"]) is not bool:
            raise AccessDenied("Human acceptance policy must be explicit")
        if (
            type(sources) is not tuple
            or any(type(source) is not BoundFile for source in sources)
            or len({source.path for source in sources}) != len(sources)
        ):
            raise AccessDenied("Distinct sealed installed code sources required")
        self.prepare, self.sources = prepare, sources
        self._bound = (self.seal, self.prepare, self.sources)

    @property
    def reference(self):
        return self.data["reference"]

    @property
    def fingerprint(self):
        value = {"policy": self.seal.original, "data": self.data}
        if self.sources:
            value["sources"] = [source.original for source in self.sources]
        return digest(value)

    def check(self):
        if (self.seal, self.prepare, self.sources) != self._bound:
            raise AccessDenied("Installed code policy implementation changed")
        self.seal.check()
        for source in self.sources:
            source.check()
        if read_private(self.seal.path) != self.data:
            raise AccessDenied("Installed code policy changed")

    def require(self, core, handle, task):
        self.check()
        if not task.code_bearing or (
            self.data["scope"],
            self.data["task_id"],
            self.data["prose_digest"],
        ) != (asdict(core.session_binding(handle)[1]), task.id, task.prose_digest):
            raise AccessDenied("Installed code policy belongs to another task or scope")
