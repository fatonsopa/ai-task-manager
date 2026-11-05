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

    def require(self):
        if type(self.profile) is not GitProfile or type(self.checks) is not CandidateChecks:
            raise AccessDenied("Actual installed Git profile and candidate checks are required")
        if type(self.required_hooks) is not tuple:
            raise AccessDenied("Immutable installed commit guard selection required")


class CodeRegistration:
    def __init__(self, policy_file, prepare):
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
        self.prepare = prepare
        self._bound = (self.seal, self.prepare)

    @property
    def reference(self):
        return self.data["reference"]

    @property
    def fingerprint(self):
        return digest({"policy": self.seal.original, "data": self.data})

    def check(self):
        if (self.seal, self.prepare) != self._bound:
            raise AccessDenied("Installed code policy implementation changed")
        self.seal.check()
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
