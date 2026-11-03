"""Commit verified actual owned work from persisted workflow facts."""

import time
from contextlib import nullcontext
from dataclasses import asdict, is_dataclass

from ai_task_manager.domains.checks.models import fingerprint
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import IssueStatus
from ai_task_manager.supervisor.models import RuntimeUncertain


class WorkerCommits:
    def __init__(self, core, handle, runtime, journal, git, *, checks=None, clock=None):
        self.core, self.handle, self.runtime = core, handle, runtime
        self.journal, self.git, self.checks = journal, git, checks
        self.clock = clock or core.context.clock
        self.binding = core.session_binding(handle)

    def _current(self, row, plan):
        if self.core.session_binding(self.handle) != self.binding:
            raise AccessDenied("Worker commit session changed")
        task = self.core.get_task(self.handle, row["task_id"])
        attempt = self.core.get_attempt(self.handle, row["attempt_id"])
        if (
            not task.code_bearing
            or task.human_hold
            or task.prose_digest != row["prose_digest"]
            or task.decision_revision != row["decision_revision"]
            or task.observed_status != IssueStatus.IN_PROGRESS
            or task.desired_status != IssueStatus.IN_PROGRESS
            or task.sync_state != "confirmed"
            or attempt.task_id != task.id
            or attempt.state != "done"
            or attempt.host != plan.host
            or row["plan_digest"] != plan.digest
            or self.runtime.binding(task, plan) != row["runtime_digest"]
        ):
            raise AccessDenied("Current worker commit authority differs from its saved workflow")
        owner = self.journal.attempt(attempt.id)
        if owner["task_id"] != task.id or owner["deadline"] != plan.deadline:
            raise AccessDenied("Worker commit original owner or deadline differs")
        pair = self.runtime.pair(attempt.id)
        fact = self.runtime.ownership(attempt.id)
        if (
            pair.attempt_id != attempt.id
            or (attempt.worktree_id, attempt.database_id) != (pair.worktree_id, pair.database_id)
            or fact["branch"] != "managed/" + attempt.id
        ):
            raise AccessDenied("Worker branch or resources belong to different work")
        return task, attempt, pair, fact

    def prepare(self, row, plan):
        task, attempt, pair, fact = self._current(row, plan)
        if self.clock() >= plan.deadline or fact["head"] != plan.base:
            raise AccessDenied("Worker source or original deadline changed")
        if "execute" not in row["steps"] or row["steps"]["execute"]["result"] is None:
            raise EvidenceRequired("Worker execution must settle before committing")
        if (
            not plan.commit_checks
            or self.checks is None
            or not callable(getattr(self.checks, "verified_result", None))
        ):
            raise EvidenceRequired("Code commits require installed verified checks")
        snapshot = self.git.snapshot(pair.worktree, plan.allowed_files)
        return {
            "task_id": task.id,
            "attempt_id": attempt.id,
            "plan_digest": plan.digest,
            "base": plan.base,
            "worktree": fact,
            "resource_ids": [pair.worktree_id, pair.database_id],
            **snapshot,
        }

    def run(self, row, plan, intent):
        task, attempt, pair, _ = self._current(row, plan)
        if self.journal.effect(attempt.id, "worker-commit-intent") is not None:
            return self.reconcile(row, plan, intent)
        if self.prepare(row, plan) != intent:
            raise AccessDenied("Worker commit input differs from saved intent")
        self.journal.intend(attempt.id, "worker-commit-intent", intent)

        def boundary():
            _, _, current_pair, current = self._current(row, plan)
            if (
                self.clock() >= plan.deadline
                or current != intent["worktree"]
                or [current_pair.worktree_id, current_pair.database_id] != intent["resource_ids"]
                or self.git.source(pair.worktree) != intent["git_source"]
                or self.git.content(pair.worktree, intent["files"]) != intent["content"]
            ):
                raise AccessDenied("Worker commit source, resources or authority changed")

        def seconds():
            return min(30, plan.deadline - self.clock())

        self.git.stage(attempt.id, pair.worktree, intent["files"], boundary, seconds())
        self.git.whitespace(attempt.id, pair.worktree, boundary, seconds())
        checks_started = time.monotonic()
        if plan.commit_checks:
            if self.checks is None:
                raise EvidenceRequired("Installed worker commit checks are unavailable")
            for reference in plan.commit_checks:
                boundary()
                self.journal.intend(
                    attempt.id,
                    "worker-check:" + reference,
                    {"reference": reference, "input": fingerprint(intent)},
                )
                started = time.monotonic()
                outcome = "failed_or_unknown"
                try:
                    self.checks.execute_registered(
                        reference,
                        pair,
                        boundary,
                        nullcontext,
                        row["id"] + ":worker-check:" + reference,
                    )
                    verified = self.checks.verified_result(attempt.id, reference)
                    evidence = asdict(verified) if is_dataclass(verified) else verified
                    outcome = "verified"
                finally:
                    self.journal.observe(
                        attempt.id,
                        "worker-check-result",
                        {
                            "reference": reference,
                            "seconds": time.monotonic() - started,
                            "outcome": outcome,
                            "evidence": fingerprint(evidence) if outcome == "verified" else None,
                        },
                    )
        boundary()
        self.journal.observe(
            attempt.id,
            "worker-checks",
            {
                "seconds": time.monotonic() - checks_started,
                "references": plan.commit_checks,
            },
        )
        # Checks may neither stage other files nor rewrite the candidate.
        staged = self.git.query(pair.worktree, "diff", "--cached", "--name-only", "-z")
        if sorted(p.decode() for p in staged.split(b"\0") if p) != intent["files"]:
            raise AccessDenied("Staged files differ from the worker commit intent")
        if self.git.query(pair.worktree, "diff", "--name-only", "-z"):
            raise AccessDenied("Checked worktree content differs from the staged candidate")
        untracked = self.git.query(
            pair.worktree, "ls-files", "--others", "--exclude-standard", "-z"
        )
        if untracked:
            raise AccessDenied("New untracked files appeared during worker commit checks")
        tree = self.git.tree(pair.worktree)
        message = (
            "Worker change for "
            + task.id
            + "\n\nTask: "
            + task.id
            + "\nAttempt: "
            + attempt.id
            + "\nWorkflow: "
            + row["id"]
            + "\nWorker-Intent: "
            + fingerprint(intent)
        )
        effect = {**intent, "tree": tree, "message": message}
        self.journal.intend(attempt.id, "worker-commit-candidate", effect)
        self.git.commit(attempt.id, pair.worktree, message, boundary, seconds())
        return self.reconcile(row, plan, intent)

    def reconcile(self, row, plan, intent):
        _, attempt, pair, fact = self._current(row, plan)
        original = self.journal.effect(attempt.id, "worker-commit-intent")
        candidate = self.journal.effect(attempt.id, "worker-commit-candidate")
        if original != intent or candidate is None:
            raise RuntimeUncertain(
                "Unfinished worker checks/staging require recovery; never replay"
            )
        if (
            fact["path"] != intent["worktree"]["path"]
            or fact["common"] != intent["worktree"]["common"]
            or fact["physical"] != intent["worktree"]["physical"]
            or [pair.worktree_id, pair.database_id] != intent["resource_ids"]
        ):
            raise RuntimeUncertain("Worker commit resource lineage differs")
        results = self.journal.observations(attempt.id, "worker-git-result")
        actual = [result for result in results if result["stage"] == "commit"]
        self.git.require_quiescent_commit(attempt.id)
        head = self.git.committed(pair.worktree, candidate)
        if head is None or (
            actual and (actual[-1]["returncode"] != 0 or actual[-1]["outcome"] != "succeeded")
        ):
            raise RuntimeUncertain("Worker commit is absent or failed; never repeat hooks blindly")
        receipt = {
            "task_id": row["task_id"],
            "attempt_id": attempt.id,
            "commit": head,
            "tree": candidate["tree"],
            "branch": fact["branch"],
            "intent_digest": fingerprint(intent),
            "state": "committed",
        }
        if not self.journal.observations(attempt.id, "worker-commit-complete"):
            self.journal.observe(attempt.id, "worker-commit-complete", receipt)
        return receipt
