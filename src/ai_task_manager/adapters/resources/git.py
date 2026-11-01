"""Observed Git worktrees, with no automatic deletion of uncommitted work."""

import subprocess
from pathlib import Path

from ai_task_manager.supervisor.models import RuntimeRefused

from .paths import identity, unchanged


def git(repository, *args):
    result = subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, text=True, timeout=20
    )
    if result.returncode:
        raise RuntimeRefused(f"Git resource operation failed: {result.stderr.strip()}")
    return result.stdout.strip()


class GitWorktrees:
    def __init__(self, repository: Path):
        self.repository = Path(repository).resolve()
        self.common = str(Path(git(self.repository, "rev-parse", "--git-common-dir")).resolve())

    def create(self, path: Path, branch: str, base: str):
        if path.exists() or path.is_symlink():
            raise RuntimeRefused("Worktree destination already exists")
        if not branch.startswith("managed/") or branch.startswith("-"):
            raise RuntimeRefused("Managed branch identity is required")
        commit = git(
            self.repository, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"
        )
        git(self.repository, "check-ref-format", f"refs/heads/{branch}")
        git(self.repository, "worktree", "add", "-b", branch, str(path), commit)
        return self.inspect(path, branch)

    def inspect(self, path: Path, branch: str):
        physical = identity(path)
        common = Path(
            git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        expected_common = Path(
            git(self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        if common != expected_common or git(path, "symbolic-ref", "--short", "HEAD") != branch:
            raise RuntimeRefused("Worktree repository or branch ownership changed")
        registered = git(self.repository, "worktree", "list", "--porcelain")
        if f"worktree {path}\n" not in registered + "\n":
            raise RuntimeRefused("Worktree is not registered in its owning repository")
        return {
            "physical": physical,
            "common": str(common),
            "branch": branch,
            "head": git(path, "rev-parse", "HEAD"),
            "path": str(path),
        }

    def remove(self, path: Path, receipt):
        unchanged(path, receipt["physical"])
        observed = self.inspect(path, receipt["branch"])
        if observed["common"] != receipt["common"]:
            raise RuntimeRefused("Worktree repository identity changed")
        if git(path, "status", "--porcelain", "--untracked-files=all"):
            raise RuntimeRefused("Uncommitted work prevents resource removal")
        git(self.repository, "worktree", "remove", str(path))
