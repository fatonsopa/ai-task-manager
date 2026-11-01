"""Apply one observed human decision before its guarded task action."""

from ai_task_manager.domains.work.errors import StaleDecision, StaleVersion


def authorize(service, task, target, action, data, invocation, proposed_digest=None):
    if data.get("human_ref"):

        def observed_plan():
            if task.version != data["expected_version"]:
                raise StaleVersion("Task version has changed")
            if task.decision_revision != data["decision_revision"]:
                raise StaleDecision("A newer human decision takes precedence")
            return service.decisions.human_plan(
                service.scope, task, target, action, data["human_ref"], proposed_digest
            )

        plan = invocation.reference("human_plan", observed_plan)
        if plan.get("already_recorded"):
            return plan["decision_ref"], data["expected_version"], data["decision_revision"]
        changed = service.core.decisions.record(
            service.handle,
            task.id,
            data["expected_version"],
            plan["human_decision_ref"],
            data["operation_key"] + ":observed-human",
        )
        return plan["decision_ref"], changed.version, changed.decision_revision
    decision = invocation.reference(
        "decision",
        lambda: service.decisions.decision(
            service.scope,
            task,
            target,
            action,
            grant_ref=service.grant_ref,
            reason=data.get("reviewed_reason", ""),
        ),
    )
    return decision, data["expected_version"], data["decision_revision"]
