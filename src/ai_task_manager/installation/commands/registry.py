"""Task-bound registered container checks; scripts are pinned before model work."""

import time
from dataclasses import dataclass
from types import MappingProxyType

from ai_task_manager.adapters.hosts.bridge.container_plan import ContainerPlan
from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.domains.checks.models import identifier, repo_path
from ai_task_manager.domains.work.errors import AccessDenied


@dataclass(frozen=True)
class CommandDefinition:
    reference: str
    script: str
    script_sha256: str
    files: tuple[str, ...]
    seconds: float = 8

    def __post_init__(self):
        identifier(self.reference)
        repo_path(self.script)
        if (
            type(self.files) is not tuple
            or self.script not in self.files
            or len(set(self.files)) != len(self.files)
            or len(self.files) > 64
            or type(self.script_sha256) is not str
            or len(self.script_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.script_sha256)
        ):
            raise AccessDenied(
                "Exact preinstalled verification source and selected inputs required"
            )
        for name in self.files:
            repo_path(name)

    def plan(self, image_id):
        value = ContainerPlan(
            image_id, ("python", "-I", self.script), self.files, seconds=self.seconds
        )
        value.check()
        return value


class InstalledCommands:
    def __init__(self, context, runner, definitions, *, check_sources, clock=time.time):
        if not isinstance(runner, DockerCommands):
            raise AccessDenied("Actual confined container capability is required")
        self.context, self.runner, self.clock = context, runner, clock
        self.definitions = MappingProxyType({d.reference: d for d in definitions})
        if len(self.definitions) != len(definitions) or not callable(check_sources):
            raise AccessDenied("Distinct source-bound installed command references required")
        self.references = tuple(self.definitions)
        if set(runner.plans) != set(self.references):
            raise AccessDenied("Runner command catalog differs from installed references")
        for name, value in self.definitions.items():
            if (
                runner.plans[name].argv != ("python", "-I", value.script)
                or runner.plans[name].files != value.files
            ):
                raise AccessDenied("Runner argv/input selection differs from installed recipe")
        self.binding = context.core.session_binding(context.handle)
        self._plans = dict(runner.plans)
        self._environment = dict(runner.environment)
        self.check_sources = check_sources
        self._bound = (context, runner, self.definitions, check_sources, clock)

    def _current(self, attempt_id):
        if any(
            a is not b
            for a, b in zip(
                (self.context, self.runner, self.definitions, self.check_sources, self.clock),
                self._bound,
            )
        ):
            raise AccessDenied("Installed command owner changed")
        if self.runner.plans != self._plans or self.runner.environment != self._environment:
            raise AccessDenied("Confined runner plan or environment changed")
        self.check_sources()
        c = self.context
        task = c.core.get_task(c.handle, c.task.id)
        attempt = c.core.get_attempt(c.handle, attempt_id)
        if (
            task.prose_digest != c.task.prose_digest
            or attempt.task_id != task.id
            or c.core.session_binding(c.handle) != self.binding
        ):
            raise AccessDenied("Command task, prose or session changed")
        return attempt

    def execute_registered(self, reference, resources, boundary, effect_boundary, operation_key):
        if reference not in self.definitions:
            raise AccessDenied("Command reference is not installed")
        attempt = self._current(resources.attempt_id)
        c = self.context
        if self.clock() >= min(c.deadline, self.runner.deadline):
            raise AccessDenied("Original registered command deadline expired")
        c.resources.verify(resources)
        if (attempt.worktree_id, attempt.database_id) != (
            resources.worktree_id,
            resources.database_id,
        ):
            raise AccessDenied("Command resources do not belong to the canonical attempt")
        source = Workspace(resources.worktree).read(self.definitions[reference].script)
        if source["sha256"] != self.definitions[reference].script_sha256:
            raise AccessDenied("Verification script changed from its installed source")
        boundary()
        return self.runner.execute_registered(
            reference, resources, boundary, effect_boundary, operation_key
        )

    def verified_result(self, attempt_id, reference):
        if reference not in self.definitions:
            raise AccessDenied("Unknown registered result")
        self._current(attempt_id)
        return self.runner.verified_result(attempt_id, reference)

    def reconcile_stopped(self, attempt_id):
        self._current(attempt_id)
        return self.runner.reconcile_stopped(attempt_id)
