"""Keep actual worker commit inputs owned through checks, Git waits and recovery."""

from contextlib import contextmanager
from dataclasses import dataclass

from ai_task_manager.application.commits import WorkerCommits
from ai_task_manager.domains.work.errors import AccessDenied


@dataclass(frozen=True)
class ResourceCommitChecks:
    delegate: object
    resources: object
    runtime: object

    def execute_registered(self, reference, pair, boundary, effect_boundary, operation_key):
        self.resources.verify(pair)
        observed = self.delegate.execute_registered(
            reference, pair, boundary, effect_boundary, operation_key
        )
        self.resources.verify(pair)
        return observed

    def verified_result(self, attempt_id, reference, *, operation_key=None):
        pair = self.runtime.pair(attempt_id)
        self.resources.verify(pair)
        observed = self.delegate.verified_result(attempt_id, reference, operation_key=operation_key)
        self.resources.verify(pair)
        return observed


class ResourceWorkerCommits:
    def __init__(self, commits, resources):
        if type(commits) is not WorkerCommits:
            raise AccessDenied("Actual current Core worker commit service required")
        self.commits, self.resources = commits, resources
        self.checks = ResourceCommitChecks(commits.checks, resources, commits.runtime)
        commits.checks = self.checks
        self._bound = (commits, resources, self.checks)

    @contextmanager
    def _use(self, row, plan):
        if any(
            a is not b
            for a, b in zip((self.commits, self.resources, self.commits.checks), self._bound)
        ):
            raise AccessDenied("Installed worker commit resource owner changed")
        # The existing full scope/task/attempt check must precede creating a fence.
        _, _, pair, _ = self.commits._current(row, plan)
        with self.resources.use(pair):
            self.resources.verify(pair)
            yield
            self.resources.verify(pair)

    def prepare(self, row, plan):
        with self._use(row, plan):
            return self.commits.prepare(row, plan)

    def run(self, row, plan, intent):
        with self._use(row, plan):
            return self.commits.run(row, plan, intent)

    def reconcile(self, row, plan, intent):
        with self._use(row, plan):
            return self.commits.reconcile(row, plan, intent)
