"""Immutable exact command references; providers and confinement are trusted ports."""

from dataclasses import dataclass

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain


@dataclass(frozen=True)
class ModelWork:
    """One reviewed full-assignment model invocation, including its tool loop."""

    task_id: str
    host: str
    prompt: str
    accounting_ref: str


@dataclass(frozen=True)
class ConfinedWork:
    task_id: str
    runner_ref: str


@dataclass(frozen=True)
class ProtectedMergeWork:
    task_id: str
    merge_ref: str


@dataclass(frozen=True)
class MergeContext:
    resources: object
    before_start: object
    spawn_boundary: object
    check_authority: object
    operation_key: str


class ReviewedExecutions:
    def __init__(self, journal, plans, accounting, confined_runner, merge_runner=None):
        self.merge_runner = merge_runner
        self.journal, self.plans, self.accounting, self.runner = (
            journal,
            dict(plans),
            accounting,
            confined_runner,
        )

    def execution_kind(self, reference, task_id):
        plan = self.plans.get(reference)
        if plan is None or plan.task_id != task_id:
            raise AccessDenied("Command reference is not registered for this exact task")
        if isinstance(plan, ModelWork):
            return "model"
        if isinstance(plan, ConfinedWork):
            return "confined"
        if isinstance(plan, ProtectedMergeWork):
            return "merge"
        raise AccessDenied("Unknown reviewed execution kind")

    def execute(
        self,
        *,
        handle,
        attempt,
        resources,
        host,
        command_ref,
        operation_key,
        boundary,
        effect_boundary,
        merge_authority=None,
    ):
        plan = self.plans.get(command_ref)
        if (
            not isinstance(plan, (ModelWork, ConfinedWork, ProtectedMergeWork))
            or plan.task_id != attempt.task_id
        ):
            raise AccessDenied("Command reference is not registered for this exact task")
        boundary()
        if isinstance(plan, ProtectedMergeWork):
            if self.merge_runner is None or merge_authority is None:
                raise AccessDenied(
                    "The existing protected foreground merge adapter is not configured"
                )
            if self.merge_runner.reconcile_stopped(attempt.id) is not True:
                raise RuntimeUncertain(
                    "Unresolved foreground merge requires the existing lead recovery route"
                )
        if not self.journal.intend(
            "execution:" + operation_key,
            {"attempt_id": attempt.id, "command_ref": command_ref, "task_id": attempt.task_id},
        ):
            raise AccessDenied(
                "Existing invocation is uncertain or complete; never launch it twice"
            )
        if isinstance(plan, ModelWork):
            if plan.host != attempt.host or not plan.accounting_ref:
                raise AccessDenied("Model plan account or host differs from the admitted worker")
            permit = self.accounting.authorize(plan.accounting_ref, attempt)
            try:
                boundary()
                result = host.execute_model_turn(plan.prompt)
            except BaseException as exc:
                self.accounting.record(
                    permit, outcome="failed_or_uncertain", detail=type(exc).__name__
                )
                raise
            self.accounting.record(permit, outcome="observed_turn", detail=None)
            return result
        if isinstance(plan, ProtectedMergeWork):
            if self.merge_runner is None or merge_authority is None:
                raise AccessDenied(
                    "The existing protected foreground merge adapter is not configured"
                )
            context = MergeContext(
                resources, boundary, effect_boundary, merge_authority, operation_key
            )
            # The adapter owns the unchanged foreground script and its expected DB
            # lifecycle. Never apply a generic database-presence check after MERGED.
            result = self.merge_runner.execute_registered(plan.merge_ref, context)
            self.journal._append(
                "trace",
                {
                    "effect": "protected_merge_result",
                    "attempt_id": attempt.id,
                    "command_ref": command_ref,
                    "result": result,
                    "source": "trusted_project_adapter",
                },
            )
            return result
        # This port must launch a reviewed credential-free confined plan. No model
        # argv, arbitrary shell or editable Python is executed in this controller.
        return self.runner.execute_registered(
            plan.runner_ref, resources, boundary, effect_boundary, operation_key
        )

    def reconcile_stopped(self, attempt_id):
        if self.runner.reconcile_stopped(attempt_id) is not True:
            return False
        if self.merge_runner is not None:
            return self.merge_runner.reconcile_stopped(attempt_id) is True
        return True
