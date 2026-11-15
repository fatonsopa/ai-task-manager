"""Typed decision publication uses the existing scoped writer, publisher and human source."""

from .models import external_result, result
from .projection import require_owned


def publisher(service, task, *, decision):
    require_owned(service, task)
    view = (
        service._required("writer").approvals_for_decisions
        if decision
        else service._required("writer").approvals_for_progress
    )(service.scope, task.id, task.prose_digest)
    value = service._required("publisher")(service.scope, task, view)

    def current():
        grant = service._required("decisions").observations.verify_enrollment(service.grant_ref)
        if grant.scope != service.scope:
            from ai_task_manager.domains.work.errors import AccessDenied

            raise AccessDenied("Publication enrollment belongs to another scope")
        service.seal.check()
        service.claim_store.check()

    value.check_authority = current
    return value, view


def prepare(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    value, _ = publisher(service, task, decision=True)
    fields = {
        key: data[key] for key in ("title", "context", "options", "recommendation", "rationale")
    }
    return result(value.prepare_decision(service.handle, task.id, fields, service.writer))


def request(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    value, view = publisher(service, task, decision=True)
    prose = view.prose(data["writing_ref"])
    return external_result(
        value.publish(service.handle, task.id, prose, data["writing_ref"], data["operation_key"])
    )


def selected(service, data):
    from ai_task_manager.adapters.linear.progress_store import ProgressStore

    row = ProgressStore(service._required("inbound").journal).get(service.scope, data["request_id"])
    if row["decision_state"] is None:
        from ai_task_manager.domains.work.errors import ApprovalRequired

        raise ApprovalRequired("Request identity belongs to routine progress")
    task = service.core.get_task(service.handle, row["task_id"])
    value, _ = publisher(service, task, decision=True)
    return value, row


def resolve(service, data, invocation):
    value, row = selected(service, data)
    return external_result(
        value.resolve_decision(
            service.handle,
            row["id"],
            data["human_ref"],
            data["operation_key"],
            service.decisions.observations,
        )
    )


def show(service, data, invocation):
    value, row = selected(service, data)
    value.check_authority()
    return result(value._decision_view(row))
