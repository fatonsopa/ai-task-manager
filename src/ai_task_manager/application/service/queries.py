"""Focused queries operations over the configured session."""

from ai_task_manager.domains.work.errors import WorkError

from .models import Unavailable, result
from .projection import quarantined, task_view


def _prepare_current_milestones(service, task):
    if not any(
        subject == task.prose_digest
        for subject, _ in getattr(service.dependencies, "milestones", ())
    ):
        return
    prepare = getattr(service.dependencies, "prepare_current_milestones", None)
    if callable(prepare) and not task.human_hold and not quarantined(service, task):
        try:
            prepare(None, task_id=task.id)
        except WorkError:
            # Projection retains the existing authoritative blocked/unknown state.
            pass


def task_list(service, data, invocation):
    tasks = service.core.list_tasks(service.handle)
    for task in tasks:
        _prepare_current_milestones(service, task)
    views = [task_view(service, task) for task in tasks]
    ordering = {item["task_id"]: index for index, item in enumerate(
        service.core.scheduling.ordered(service.handle)
    )}
    views.sort(key=lambda item: ordering.get(item["id"], len(ordering)))
    visible = [view for view in views if not view["quarantined"]]
    envelope = result(visible)
    envelope["quarantined_count"] = len(tasks) - len(visible)
    return envelope


def task_show(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    _prepare_current_milestones(service, task)
    return result(task_view(service, task))


def task_external_changes(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    external = getattr(service.core, "external", None)
    if external is None:
        raise Unavailable("External observation integration is unavailable")
    rows = external.observations(service.handle, data["task_id"])
    if quarantined(service, task):
        rows = [{key: row[key] for key in ("id", "source_revision", "state")} for row in rows]
    return result(rows)
