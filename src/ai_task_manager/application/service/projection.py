"""Current task views retain audit facts without presenting foreign work as active."""

import json
from dataclasses import asdict

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS


def quarantined(service, task):
    external = getattr(service.core, "external", None)
    if external is None:
        return False
    observations = external.observations(service.handle, task.id)
    if not observations:
        return False
    latest_revision = max(item["source_revision"] for item in observations)
    latest = [
        json.loads(item["payload"])
        for item in observations
        if item["source_revision"] == latest_revision and item["state"] != "superseded"
    ]
    return any(item.get("assigned_to_owner") is False for item in latest)


def require_owned(service, task):
    if quarantined(service, task):
        raise AccessDenied("Task left the managed scope; reconcile its retained ownership")


def task_view(service, task):
    if quarantined(service, task):
        return {
            "id": task.id,
            "issue_id": task.issue_id,
            "title": "Task outside managed scope",
            "body": None,
            "observed_status": None,
            "desired_status": None,
            "execution_state": "quarantined",
            "sync_state": task.sync_state,
            "quarantined": True,
            "active_group": False,
            "resource_reconciliation_required": task.execution_state in ACTIVE_ATTEMPTS,
        }
    data = asdict(task)
    data["quarantined"], data["active_group"] = False, True
    if task.execution_state in ACTIVE_ATTEMPTS and (
        task.human_hold or task.sync_state != "confirmed"
    ):
        data["last_observed_execution_state"] = task.execution_state
        data["execution_state"] = "stopping_or_unresolved"
        data["work_authorized"] = False
    return data


def attempt_view(service, attempt):
    task = service.core.get_task(service.handle, attempt.task_id)
    if quarantined(service, task):
        return {
            "id": attempt.id,
            "task_id": task.id,
            "state": "quarantined",
            "resource_reconciliation_required": attempt.state in ACTIVE_ATTEMPTS,
        }
    data = asdict(attempt)
    if attempt.state in ACTIVE_ATTEMPTS and (task.human_hold or task.sync_state != "confirmed"):
        data["last_observed_state"] = attempt.state
        data["state"] = "stopping_or_unresolved"
        data["work_authorized"] = False
    return data


def event_view(service, event):
    data = asdict(event)
    if event.task_id:
        task = service.core.get_task(service.handle, event.task_id)
        if quarantined(service, task):
            data["payload"] = {"quarantined": True, "ownership_reconciliation_required": True}
    return data
