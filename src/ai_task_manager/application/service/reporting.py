"""Restrict a fresh reporting enrollment to one retained event."""

import time
from dataclasses import asdict

from ai_task_manager.adapters.decisions.observations import current_enrollment, identity
from ai_task_manager.adapters.workflow_control.identity import source_digest
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied


class ReportingBoundary:
    def __init__(self, journal, reference):
        self.journal, self.reference = journal, reference

    def check(self, service, operation, data):
        if operation not in {"session_show", "event_list", "event_acknowledge"}:
            raise AccessDenied("Reporting enrollment permits only its selected event and receipts")
        record = current_enrollment(self.journal, self.reference, asdict(service.scope))
        if (
            record.get("kind") != "event_reporting"
            or record["actions"]
            or time.time() >= record["expires_at"]
        ):
            raise AccessDenied("Current finite reporting authority is required")
        if record["source_digest"] != source_digest():
            raise AccessDenied("Reporting product source changed")
        for item in record["sources"]:
            if identity(item["path"], content="sha256" in item) != item:
                raise AccessDenied("Original reporting source or store changed")
        if data.get("event_id", record["event_id"]) != record["event_id"]:
            raise AccessDenied("Reporting receipt belongs to another event")
        service.decisions.observations.verify_enrollment(self.reference)
        service.decisions.observations.client.check_connection(service.scope, refresh=True)
        event = service.core.notifications.get_event(service.handle, record["event_id"])
        task = service.core.get_task(service.handle, event.task_id)
        from .projection import require_owned

        require_owned(service, task)
        if (
            digest(asdict(event)) != record["event_digest"]
            or task.id != record["task_id"]
            or task.version != record["task_version"]
            or task.sync_state != "confirmed"
            or getattr(task, "decision_revision", 0) != record["decision_revision"]
        ):
            raise AccessDenied("Current event, task version or ownership changed")
        return record
