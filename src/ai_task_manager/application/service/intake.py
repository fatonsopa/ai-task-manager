"""Focused intake operations over the configured session."""

from ai_task_manager.domains.work.errors import InvalidTransition
from ai_task_manager.domains.work.models import TaskProse
from ai_task_manager.storage import issue_claims

from .authorization import authorize
from .models import Unavailable, external_result, result
from .projection import require_owned, task_view


def writing_prepare(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"]) if data.get("task_id") else None
    if task:
        require_owned(service, task)
    prose = TaskProse(
        data["title"], data["body"], data["code_bearing"], tuple(data["required_work"])
    )
    return result(
        service._required("writer").prepare(
            service.scope, data["kind"], prose, purpose=data["purpose"], task=task
        )
    )


def writing_review(service, data, invocation):
    return result(
        service._required("writer").review(service.scope, data["writing_id"], data["review"])
    )


def writing_finalize(service, data, invocation):
    return result(service._required("writer").finalize(service.scope, data["writing_id"]))


def task_submit(service, data, invocation):
    view = service._required("writer").approvals_for_tasks(service.scope)
    prose = view.prose(data["writing_ref"])
    followup_ref = data.get("followup_ref")
    if data.get("parent_task_id"):
        parent = service.core.get_task(service.handle, data["parent_task_id"])
        require_owned(service, parent)
        if followup_ref is None:

            def issue_followup():
                # Reuse Core's approval and canonical delivery predicates. Provider
                # enrollment checks occur only after this local transaction closes.
                _, approved_digest = service.core.intake.approved(
                    prose, data["writing_ref"], service.scope
                )
                with service.core.context.db.transaction() as conn:
                    if not issue_claims.known_delivered(conn, service.scope, parent):
                        raise InvalidTransition(
                            "Delivered-issue follow-up must link a delivered task"
                        )
                return service._required("decisions").delivered_followup(
                    service.scope, parent, approved_digest, grant_ref=service.grant_ref
                )

            followup_ref = invocation.reference("delivered_followup", issue_followup)
    task = service.core.intake.submit(
        service.handle,
        prose,
        data["writing_ref"],
        data["operation_key"],
        data.get("parent_task_id"),
        followup_ref,
    )
    return service.confirm(task)


def task_revise(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    require_owned(service, task)
    view = service._required("writer").approvals_for_tasks(service.scope)
    prose = view.prose(data["writing_ref"])
    service._required("decisions")
    decision, version, revision = authorize(
        service,
        task,
        task.observed_status,
        "revise",
        data,
        invocation,
        view.verify_approval(data["writing_ref"]).digest,
    )
    return service.confirm(
        service.core.intake.revise(
            service.handle,
            task.id,
            prose,
            data["writing_ref"],
            version,
            revision,
            decision,
            data["operation_key"],
        )
    )


def task_progress(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    require_owned(service, task)
    view = service._required("writer").approvals_for_progress(
        service.scope, task.id, task.prose_digest
    )
    prose = view.prose(data["writing_ref"])
    publisher = service._required("publisher")(service.scope, task, view)
    return external_result(
        publisher.publish(
            service.handle, task.id, prose, data["writing_ref"], data["operation_key"]
        )
    )


def task_accept_external_prose(service, data, invocation):
    require_owned(service, service.core.get_task(service.handle, data["task_id"]))
    view = service._required("writer").approvals_for_tasks(service.scope)
    prose = view.prose(data["writing_ref"])
    readback = service._required("inbound").fresh_readback_ref(
        service.handle, data["task_id"], data["observation_id"]
    )
    external = getattr(service.core, "external", None)
    if external is None:
        raise Unavailable("External prose approval integration is unavailable")
    changed = external.approve_prose_revision(
        service.handle,
        data["task_id"],
        data["observation_id"],
        prose,
        data["writing_ref"],
        data["expected_version"],
        data["decision_revision"],
        readback,
        data["operation_key"],
    )
    return result(
        task_view(service, changed), "confirmed" if changed.sync_state == "confirmed" else "pending"
    )
