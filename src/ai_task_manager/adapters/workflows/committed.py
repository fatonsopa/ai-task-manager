"""Observe the controller's exact committed worker output outside Core transactions."""

import subprocess
from dataclasses import replace

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.domains.checks.models import fingerprint, repo_path
from ai_task_manager.domains.work.errors import EvidenceRequired

from .repository import RepositoryInspector


class CommittedRepositoryInspector(RepositoryInspector):
    def __init__(self, runtime, receipt):
        super().__init__(runtime)
        self.receipt = receipt

    def inspect(self, task, attempt, plan):
        receipt = self.receipt(attempt.id)
        if not receipt or (receipt["task_id"], receipt["attempt_id"], receipt["state"]) != (
            task.id,
            attempt.id,
            "committed",
        ):
            raise EvidenceRequired("A retained exact controller worker-commit receipt is required")
        pair = self.runtime.pair(attempt.id)
        if (
            self.runtime.ownership(attempt.id)["head"] != receipt["commit"]
            or git(pair.worktree, "rev-parse", "HEAD^{tree}") != receipt["tree"]
        ):
            raise EvidenceRequired("Committed source differs from the retained worker receipt")
        observed = super().inspect(task, attempt, replace(plan, base=receipt["commit"]))
        if observed["files"]:
            raise EvidenceRequired("Uncommitted source cannot receive code-work acceptance")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(pair.worktree),
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                plan.base,
                receipt["commit"],
                "--",
            ],
            capture_output=True,
            timeout=5,
        )
        if result.returncode or len(result.stdout) > 262144:
            raise EvidenceRequired("Complete committed file inventory is unavailable")
        changed = tuple(p.decode("utf-8", "strict") for p in result.stdout.split(b"\0") if p)
        for path in changed:
            repo_path(path)
        if not changed or not set(changed) <= set(plan.allowed_files):
            raise EvidenceRequired("Committed files exceed the trusted workflow scope")
        body = {k: v for k, v in observed.items() if k != "digest"}
        body["files"] = sorted(changed)
        body["worker_commit"] = receipt
        return dict(body, digest=fingerprint(body))
