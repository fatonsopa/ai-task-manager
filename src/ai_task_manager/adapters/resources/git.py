"""Observed Git worktrees, with no automatic deletion of uncommitted work."""

import subprocess
from pathlib import Path

from ai_task_manager.supervisor.models import RuntimeRefused

from .paths import identity, unchanged
from .release_git import release_git


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
        self.common = str(Path(git(
            self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )).resolve())

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

    def inspect(self, path: Path, branch: str, *, release=False):
        query = release_git if release else git
        physical = identity(path)
        common = Path(
            query(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        expected_common = Path(
            query(self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        if common != expected_common or query(path, "symbolic-ref", "--short", "HEAD") != branch:
            raise RuntimeRefused("Worktree repository or branch ownership changed")
        if release and Path(query(path, "rev-parse", "--show-toplevel")).resolve() != path:
            raise RuntimeRefused("Worktree content path changed")
        registered = query(self.repository, "worktree", "list", "--porcelain")
        if f"worktree {path}\n" not in registered + "\n":
            raise RuntimeRefused("Worktree is not registered in its owning repository")
        return {
            "physical": physical,
            "common": str(common),
            "branch": branch,
            "head": query(path, "rev-parse", "HEAD"),
            "path": str(path),
        }

    def require_clean(self, path: Path):
        entries = release_git(path, "ls-files", "--stage", "-z")
        if any(entry.startswith("160000 ") for entry in entries.split("\0")):
            raise RuntimeRefused("Git submodules require confined resource release")
        if release_git(
            path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"
        ):
            raise RuntimeRefused("Uncommitted work prevents resource removal")

    def registered_worktrees(self):
        return release_git(self.repository, "worktree", "list", "--porcelain")

    def remove(self, path: Path, receipt):
        unchanged(path, receipt["physical"])
        observed = self.inspect(path, receipt["branch"], release=True)
        if observed["common"] != receipt["common"]:
            raise RuntimeRefused("Worktree repository identity changed")
        self.require_clean(path)
        release_git(self.repository, "worktree", "remove", str(path), worktree=path)
