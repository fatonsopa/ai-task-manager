"""Completed-worker merge permission is independent of permission to run worker tools."""

from contextlib import contextmanager

from ai_task_manager.application.merging.authority import resource_alias
from ai_task_manager.domains.work.errors import AccessDenied


class CoreCandidateAuthority:
    def __init__(self, core, handle, resources, pair):
        self.core, self.handle, self.resources, self.pair = core, handle, resources, pair

    def registration(self, grant):
        _, scope = self.core.session_binding(self.handle)
        binding = grant.binding
        self.core.get_task(self.handle, binding.task_id)
        if (scope.principal, scope.project, scope.logical_session) != (
            binding.principal_id,
            binding.project_id,
            binding.session_id,
        ):
            raise AccessDenied("Candidate belongs to another scoped merge owner")

    def _snapshot(self, grant, task, attempt, scope):
        binding = grant.binding
        if (
            (scope.principal, scope.project, scope.logical_session)
            != (binding.principal_id, binding.project_id, binding.session_id)
            or task.id != binding.task_id
            or task.prose_digest != binding.prose_digest
            or attempt.id != binding.attempt_id
            or attempt.worktree_id != self.pair.worktree_id
            or attempt.database_id != self.pair.database_id
            or self.pair.attempt_id != attempt.id
            or binding.worktree_id != resource_alias("worktree", attempt.worktree_id)
            or binding.resource_id != resource_alias("database", attempt.database_id)
        ):
            raise AccessDenied("Candidate task/source/resource ownership differs")

    @contextmanager
    def boundary(self, grant):
        with self.core.execution.merge_snapshot_boundary(
            self.handle,
            grant.binding.task_id,
            grant.binding.attempt_id,
            grant.task_version,
            grant.attempt_version,
            grant.decision_revision,
            grant.decision_ref,
        ) as snapshot:
            self._snapshot(grant, *snapshot)
            yield

    def check_current(self, grant):
        self.registration(grant)
        with self.boundary(grant):
            pass
        self.resources.verify(self.pair)

    def check_source(self, grant, record):
        fact = self.resources.verify(self.pair)["worktree"]
        if (
            record["path"] != str(self.pair.worktree)
            or record["common"] != fact["common"]
            or tuple(record["identity"]) != (fact["physical"]["device"], fact["physical"]["inode"])
            or record["worktree_id"] != grant.binding.worktree_id
            or record["repository_id"] != grant.binding.repository_id
        ):
            raise AccessDenied("Configured candidate source differs from actual Core resources")
