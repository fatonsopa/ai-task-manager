"""Keep actual worker commit inputs owned through checks, Git waits and recovery."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass

from ai_task_manager.application.commits import WorkerCommits
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.sessions import TaskSession


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
    def __init__(self, commits, resources, freshness=None):
        if type(commits) is not WorkerCommits:
            raise AccessDenied("Actual current Core worker commit service required")
        self.commits, self.resources = commits, resources
        self.freshness = freshness
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
        if self.freshness is not None:
            self.freshness.start(row)
        options = (
            {"observe": lambda: self._observe_use(row, plan)}
            if isinstance(self.commits.binding[0], TaskSession) else {}
        )
        with self.resources.use(pair, **options):
            self.resources.verify(pair)
            yield
            self.resources.verify(pair)

    def _observe_use(self, row, plan):
        task, attempt, _, _ = self.commits._current(row, plan)
        session, scope = self.commits.core.session_binding(self.commits.handle)
        if not isinstance(session, TaskSession) or self.commits.clock() >= plan.deadline:
            raise AccessDenied("Current successor resource use is unavailable")
        return {
            "capability": session.reference,
            "task_id": task.id,
            "attempt_id": attempt.id,
            "source_attempt": self.commits.runtime.resource_owner(attempt.id),
            "scope": asdict(scope),
            "purpose": "worker_commit",
            "deadline": plan.deadline,
        }

    def prepare(self, row, plan):
        with self._use(row, plan):
            return self.commits.prepare(row, plan)

    def run(self, row, plan, intent):
        with self._use(row, plan):
            return self.commits.run(row, plan, intent)

    def reconcile(self, row, plan, intent):
        with self._use(row, plan):
            return self.commits.reconcile(row, plan, intent)
