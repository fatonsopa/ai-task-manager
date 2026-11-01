"""Resolve actual host enrollment separately from human and transcript observations."""

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope

from .human import HumanReceipts
from .observations import NotificationReceipts

ACTIONS = frozenset(
    {
        "intake",
        "enqueue",
        "dequeue",
        "start",
        "hold",
        "cancel",
        "duplicate",
        "merge",
        "stage",
        "release",
        "non_code_complete",
        "request_review",
        "revise",
        "resume",
    }
)


class HostObservations:
    def __init__(self, journal):
        self.journal = journal
        self._human, self._notifications = HumanReceipts(journal), NotificationReceipts(journal)

    def verify_enrollment(self, ref):
        record = self.journal._read(ref, "enrollment")
        data = record["payload"]
        source = self.journal._read(data["startup_ref"], "startup")
        scope = Scope(**data["scope"])
        if (
            source["payload"]["scope"] != data["scope"]
            or scope.logical_session != data["logical_session"]
            or not set(data["actions"]) <= ACTIONS
        ):
            raise AccessDenied("Enrollment does not match actual observed host authority")
        return HostGrant(
            scope, data["logical_session"], tuple(data["actions"]), ref, record["expires_at"]
        )

    def verify_instruction(self, ref):
        return self._human.verify_instruction(ref)

    def verify_host_observation(self, ref):
        return self._notifications.verify_notification(ref)
