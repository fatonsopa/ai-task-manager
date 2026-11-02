"""Discovery is readable; admission consumes independent selection and prose authority."""

from .models import result
from .projection import task_view


def task_find(service, data, invocation):
    return result(
        service._required("existing").find(data["query"], data.get("limit", 20), data.get("cursor"))
    )


def task_inspect(service, data, invocation):
    return result(service._required("existing").inspect(data["issue"]))


def task_admit(service, data, invocation):
    prior = service.core.existing.recover(
        service.handle,
        data["discovery_ref"],
        data["writing_ref"],
        data["selection_ref"],
        data["operation_key"],
    )
    if prior is not None:
        return result(task_view(service, prior))
    prose = (
        service._required("writer").approvals_for_tasks(service.scope).prose(data["writing_ref"])
    )
    task = service._required("existing").admit(
        data["discovery_ref"],
        prose,
        data["writing_ref"],
        data["selection_ref"],
        data["operation_key"],
    )
    return result(task_view(service, service.core.get_task(service.handle, task.id)))
