"""Scoped recovery observations do not grant an attempt or settle an effect."""

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.application.service.models import result
from ai_task_manager.domains.work.errors import AccessDenied


def current_grant(service):
    grant = service._required("decisions").observations.verify_enrollment(service.grant_ref)
    if (
        type(grant) is not HostGrant
        or grant.scope != service.scope
        or grant.logical_session != service.scope.logical_session
        or grant.expires_at <= service.core.context.clock()
    ):
        raise AccessDenied("Current same-session operator enrollment required")
    return grant


def inspect_row(core, handle, row, operation_key):
    task = core.get_task(handle, row["task_id"])
    return result({
        "workflow": row["id"],
        "task_id": task.id,
        "operation_key": operation_key,
        "plan_ref": row["plan_id"],
        "plan_digest": row["plan_digest"],
        "deadline": row["plan"]["deadline"],
        "attempt_id": row["attempt_id"],
        "task_version": task.version,
        "decision_revision": task.decision_revision,
        "prose_digest": task.prose_digest,
        "status": task.observed_status,
        "action_availability": {
            "inspect": {"callable": True, "effect_eligibility": "no_effects"},
            "resume": {"callable": True, "effect_eligibility": "requires_workflow_guards"},
            **{action: {"callable": False, "blocker": "not_installed"}
               for action in ("stop", "successor", "retire")},
        },
        "steps": {name: "pending" if step["result"] is None else "recorded"
                  for name, step in row["steps"].items()},
    })
