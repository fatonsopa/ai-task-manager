"""Focused queries operations over the configured session."""

from .models import Unavailable, result
from .projection import quarantined, task_view


def task_list(service, data, invocation):
    tasks = service.core.list_tasks(service.handle)
    views = [task_view(service, task) for task in tasks]
    visible = [view for view in views if not view["quarantined"]]
    envelope = result(visible)
    envelope["quarantined_count"] = len(tasks) - len(visible)
    return envelope


def task_show(service, data, invocation):
    return result(task_view(service, service.core.get_task(service.handle, data["task_id"])))


def task_external_changes(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    external = getattr(service.core, "external", None)
    if external is None:
        raise Unavailable("External observation integration is unavailable")
    rows = external.observations(service.handle, data["task_id"])
    if quarantined(service, task):
        rows = [{key: row[key] for key in ("id", "source_revision", "state")} for row in rows]
    return result(rows)
