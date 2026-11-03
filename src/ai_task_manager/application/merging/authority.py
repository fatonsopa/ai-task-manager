"""Bind generic publication to real current Core authority and observed resources."""

import time
from contextlib import contextmanager
from dataclasses import dataclass

from ai_task_manager.config.files import digest
from ai_task_manager.domains.merging.contracts import Binding, Publication
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion


def resource_alias(kind, value):
    """Trusted composition translates an exact path-bearing ID into an opaque alias."""
    return kind + ":" + digest(value)


@dataclass(frozen=True)
class LeaseGrant:
    binding: Binding
    publication: Publication
    task_version: int
    attempt_version: int
    decision_revision: int


class CoreAuthority:
    def __init__(self, core, handle, grant, resources, pair, profile):
        if type(grant) is not LeaseGrant:
            raise AccessDenied("A trusted immutable lease grant is required")
        grant.binding.validate()
        grant.publication.validate()
        self.core, self.handle, self.grant = core, handle, grant
        self.resources, self.pair, self.profile = resources, pair, profile
        self.timings = []

    def _scope(self, binding, scope):
        if binding != self.grant.binding or (
            scope.principal,
            scope.project,
            scope.logical_session,
        ) != (binding.principal_id, binding.project_id, binding.session_id):
            raise AccessDenied("Publication belongs to a different trusted scope")

    def registration(self, binding):
        # Scope-only history reads remain possible after the work lease ends.
        _, scope = self.core.session_binding(self.handle)
        self._scope(binding, scope)

    def _snapshot(self, binding, attempt, task, scope):
        self._scope(binding, scope)
        if task.version != self.grant.task_version or attempt.version != self.grant.attempt_version:
            raise StaleVersion("Publication task or attempt version changed")
        if (
            task.id != binding.task_id
            or task.prose_digest != binding.prose_digest
            or attempt.id != binding.attempt_id
            or attempt.task_id != task.id
            or task.session_id != attempt.session_id
            or task.decision_revision != self.grant.decision_revision
            or attempt.decision_revision != self.grant.decision_revision
            or attempt.worktree_id != self.pair.worktree_id
            or attempt.database_id != self.pair.database_id
            or self.pair.attempt_id != attempt.id
            or binding.worktree_id != resource_alias("worktree", attempt.worktree_id)
            or binding.resource_id != resource_alias("database", attempt.database_id)
        ):
            raise AccessDenied("Task, prose, attempt or resource binding differs")

    def before_start(self, binding):
        self.registration(binding)  # MergeService immediately performs check_current.

    def check_current(self, binding):
        started = time.monotonic()
        self.registration(binding)
        attempt = self.core.execution.validate_work_lease(
            self.handle, binding.attempt_id, self.grant.attempt_version
        )
        task = self.core.get_task(self.handle, binding.task_id)
        _, scope = self.core.session_binding(self.handle)
        self._snapshot(binding, attempt, task, scope)
        # Actual physical/resource and Git observations never run under a Core lock.
        self.resources.verify(self.pair)
        record = self.profile.validate(binding.worktree_id, binding.repository_id)
        if record["configuration_digest"] != binding.configuration_digest or (
            record["path"] != str(self.pair.worktree)
            or self.profile.reference(record["path"], record["target_ref"])
            != self.grant.publication.old
            or self.profile.reference(record["path"], self.grant.publication.source_ref)
            != self.grant.publication.new
        ):
            raise AccessDenied("Physical publication source or target changed")
        self.timings.append(
            {"stage": "authority_and_resources", "seconds": time.monotonic() - started}
        )

    @contextmanager
    def authority_boundary(self, binding):
        self.check_current(binding)
        with self.core.execution.work_snapshot_boundary(
            self.handle, binding.attempt_id, self.grant.attempt_version
        ) as snapshot:
            self._snapshot(binding, *snapshot)
            yield
