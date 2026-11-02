"""Focused workers operations over the configured session."""

from .authorization import authorize
from .models import result
from .projection import attempt_view, require_owned


def worker_start(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    require_owned(service, task)
    service._required("decisions")
    decision, version, revision = authorize(service, task, "In Progress", "start", data, invocation)
    if service.dependencies is not None:
        service._required("workers").bind_dependencies(service.dependencies)
        service.dependencies.refresh_for_start(
            task.id,
            data["operation_key"],
            expected_version=version,
            decision_revision=revision,
        )
    source = {}
    if service.core.existing.is_imported(service.handle, task.id):
        source["source_readbacks"] = service._required("existing").worker_readbacks(
            task.id, data["operation_key"]
        )
    attempt = service._required("workers").start(
        service.handle,
        task.id,
        data["host"],
        expected_version=version,
        decision_revision=revision,
        decision_ref=decision,
        operation_key=data["operation_key"],
        **source,
    )
    current = service.core.get_attempt(service.handle, attempt.id)
    return result(attempt_view(service, current))


def worker_execute(service, data, invocation):
    attempt = service.core.get_attempt(service.handle, data["attempt_id"])
    require_owned(service, service.core.get_task(service.handle, attempt.task_id))
    return result(
        service._required("workers").execute_reviewed(
            service.handle, data["attempt_id"], data["command_ref"], data["operation_key"]
        )
    )
