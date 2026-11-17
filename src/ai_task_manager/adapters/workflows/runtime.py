"""Closed worker composition over existing guarded supervisors; no policy SQL."""

from types import MappingProxyType

from ai_task_manager.domains.checks.models import fingerprint
from ai_task_manager.supervisor.accounting import command_digest
from ai_task_manager.supervisor.models import Command, RuntimeRefused, RuntimeUncertain


class LocalCommands:
    """Trusted mechanical fixture templates, not an alternative model launcher."""

    def __init__(self, templates):
        self.templates = MappingProxyType(
            {
                ref: (tuple(argv), MappingProxyType(dict(env)), timeout)
                for ref, (argv, env, timeout) in templates.items()
            }
        )
        self.bound = set()

    def binding(self, reference):
        if reference not in self.templates:
            raise RuntimeRefused("Reviewed command is unavailable")
        argv, env, timeout = self.templates[reference]
        return fingerprint({"argv": argv, "environment": dict(env), "timeout": timeout})

    def resolve(self, reference, resources):
        if reference not in self.templates:
            raise RuntimeRefused("Reviewed workflow command is not configured")
        argv, environment, timeout = self.templates[reference]
        command = Command(tuple(argv), resources.worktree, dict(environment), timeout)
        self.bound.add(command_digest(command))
        return command

    def check(self, command):
        if command.host != "fixture" or command_digest(command) not in self.bound:
            raise RuntimeRefused("Exact owned mechanical command is not registered")
        if any(
            any(word in name.upper() for word in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))
            for name in command.environment
        ):
            raise RuntimeRefused("Mechanical commands cannot receive credentials")


class LocalSupervisorPort:
    """Actual Core/resource/process integration, explicitly mechanical fixture host only."""

    def __init__(self, supervisor, commands):
        self.supervisor, self.commands = supervisor, commands
        self.dispatches = {}
        supervisor.accounting = commands

    def binding(self, task, plan):
        return self.commands.binding(plan.command_ref)

    def start(self, handle, task, plan, decision_ref, key):
        if plan.host != "fixture":
            raise RuntimeRefused("Local mechanical port cannot substitute for a model host")
        dispatch = self.supervisor.start(
            handle,
            task.id,
            plan.host,
            expected_version=task.version,
            decision_revision=task.decision_revision,
            decision_ref=decision_ref,
            operation_key=key,
            deadline=plan.deadline,
            base=plan.base,
        )
        self.dispatches[dispatch.attempt_id] = dispatch
        return self.supervisor.admit(handle, dispatch, operation_key=key + ":admit")

    def pair(self, attempt_id):
        dispatch = self.dispatches.get(attempt_id)
        if dispatch is None:
            raise RuntimeUncertain("Actual worker handle is unavailable; no relaunch is admitted")
        self.supervisor.resources.verify(dispatch.resources)
        return dispatch.resources

    def execute(self, handle, attempt, plan, key):
        pair = self.pair(attempt.id)
        command = self.commands.resolve(plan.command_ref, pair)
        return self.supervisor.execute(
            handle, self.dispatches[attempt.id], command, operation_key=key
        )

    def stop(self, handle, attempt, key):
        self.pair(attempt.id)
        return self.supervisor.stop(handle, self.dispatches[attempt.id], operation_key=key)

    def ownership(self, attempt_id):
        pair = self.pair(attempt_id)
        fact = self.supervisor.resources.verify(pair)
        return self.supervisor.resources.git.inspect(pair.worktree, fact["worktree"]["branch"])


class ManagedWorkerPort:
    """The production native owner supplies startup, accounting and restrictions."""

    def __init__(self, workers, bindings=None):
        self.workers, self.bindings = workers, bindings

    def binding(self, task, plan):
        if self.bindings is None:
            raise RuntimeRefused("Durable native execution binding is not configured")
        return self.bindings(task, plan)

    def start(self, handle, task, plan, decision_ref, key):
        return self.workers.start(
            handle,
            task.id,
            plan.host,
            expected_version=task.version,
            decision_revision=task.decision_revision,
            decision_ref=decision_ref,
            operation_key=key,
        )

    def pair(self, attempt_id):
        dispatch = self.workers.dispatches.get(attempt_id)
        if dispatch is None:
            stops = self.workers.journal.observations(attempt_id, "runtime_outcome")
            complete = [row for row in stops if row.get("managed_stop") is True]
            successor = getattr(self.workers.resources, "completed_successors", frozenset())
            if attempt_id in successor:
                complete = stops
            if not complete or complete[-1].get("quiescent") is not True:
                raise RuntimeUncertain("Actual native owner is unavailable")
            # Reconstruct only resources of a completely stopped attempt. This
            # creates no dispatch, child handle, lease or permission to execute.
            return self.workers.resources.reconcile(attempt_id)
        self.workers.resources.verify(dispatch.resources)
        return dispatch.resources

    def execute(self, handle, attempt, plan, key):
        return self.workers.execute_reviewed(handle, attempt.id, plan.command_ref, key)

    def stop(self, handle, attempt, key):
        return self.workers.stop(handle, attempt.id, operation_key=key)

    def ownership(self, attempt_id):
        pair = self.pair(attempt_id)
        fact = self.workers.resources.verify(pair)["worktree"]
        return self.workers.resources.git.inspect(pair.worktree, fact["branch"])
