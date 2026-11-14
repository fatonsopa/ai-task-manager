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
    decision_ref: str
    publication_context: object = None
    source_binding: Binding | None = None


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
            or (self.grant.source_binding or binding).worktree_id
            != resource_alias("worktree", attempt.worktree_id)
            or binding.resource_id != resource_alias("database", attempt.database_id)
        ):
            raise AccessDenied("Task, prose, attempt or resource binding differs")

    def before_start(self, binding):
        self.registration(binding)  # MergeService immediately performs check_current.

    def check_current(self, binding):
        started = time.monotonic()
        self.registration(binding)
        with self._merge_snapshot(binding) as (task, attempt, scope):
            self._snapshot(binding, attempt, task, scope)
        # Actual physical/resource and Git observations never run under a Core lock.
        self.resources.verify(self.pair)
        record = self.profile.validate(binding.worktree_id, binding.repository_id)
        source = self.grant.source_binding
        if source is None:
            if record["path"] != str(self.pair.worktree):
                raise AccessDenied("Publication worktree differs from author resources")
        else:
            from dataclasses import replace

            if (
                self.grant.publication.mode != "fast_forward"
                or replace(
                    binding,
                    worktree_id=source.worktree_id,
                    configuration_digest=source.configuration_digest,
                    operation_key=source.operation_key,
                )
                != source
            ):
                raise AccessDenied("Publication cannot change original author binding")
            author = self.profile.validate(source.worktree_id, source.repository_id)
            if (
                author["path"] != str(self.pair.worktree)
                or author["configuration_digest"] != source.configuration_digest
                or (record["common"], record["common_identity"], record["target_ref"])
                != (author["common"], author["common_identity"], author["target_ref"])
            ):
                raise AccessDenied("Publication target differs from the owned author repository")
        if record["configuration_digest"] != binding.configuration_digest or (
            self.profile.reference(record["path"], record["target_ref"])
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
        if self.grant.publication_context is None:
            raise AccessDenied("A durable pending publication fence is required before release")
        self.check_current(binding)
        with self._merge_snapshot(binding) as (task, attempt, scope):
            self._snapshot(binding, attempt, task, scope)
            yield

    def _merge_snapshot(self, binding):
        context = self.grant.publication_context
        if context is not None and (
            context.binding_digest != binding.fingerprint
            or context.commit != self.grant.publication.new
        ):
            raise AccessDenied("Publication fence belongs to another exact operation")
        return self.core.execution.merge_snapshot_boundary(
            self.handle,
            binding.task_id,
            binding.attempt_id,
            self.grant.task_version,
            self.grant.attempt_version,
            self.grant.decision_revision,
            self.grant.decision_ref,
            publication_context=context,
        )
