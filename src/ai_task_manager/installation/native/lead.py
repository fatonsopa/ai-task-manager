"""Connect an already owned native lead through its separate operator enrollment."""

from dataclasses import dataclass

from ai_task_manager.adapters.hosts.bridge.lead import LeadToolGateway
from ai_task_manager.adapters.hosts.bridge.observations import NotificationObserver
from ai_task_manager.adapters.hosts.bridge.public_output import TerminalOutput
from ai_task_manager.adapters.hosts.bridge.startup import StartupObserver
from ai_task_manager.adapters.hosts.bridge.transcript import OwnedTranscript
from ai_task_manager.config.managed import ManagedServiceSlot
from ai_task_manager.domains.work.errors import AccessDenied


@dataclass(frozen=True)
class LeadConnection:
    slot: ManagedServiceSlot
    notifications: NotificationObserver
    gateway: LeadToolGateway
    output: TerminalOutput

    def close(self):
        """The caller retains ownership of the native host and its finite invocation."""
        self.notifications.transcript.unlisten(self.notifications._observe)
        self.output.close()


def connect_lead(
    config_file, transcript, startup, lead_enrollment_ref, terminal_fd, **trusted_factories
):
    """Trusted installed connect API; never a model tool or a host-launch fallback.

    The pinned host owner supplies its actual transport/startup and the independent
    operator enrollment captured for that startup. This consumes no launch permission,
    sends no model input, and cannot turn an assigned worker into the lead.
    """
    if (
        type(transcript) is not OwnedTranscript
        or type(startup) is not StartupObserver
        or startup.transcript is not transcript
    ):
        raise AccessDenied("The actual owned native startup and transcript are required")
    slot = ManagedServiceSlot.configured(config_file, transcript.journal, **trusted_factories)
    output = TerminalOutput(transcript.journal, terminal_fd)
    notifications = None
    try:
        notifications = NotificationObserver(transcript, startup, output)
        from ai_task_manager.config.files import read_private

        operations = (
            ("session_show", "event_list")
            if read_private(config_file).get("reporting_ref")
            else ("session_show", "event_list", "task_list", "task_show")
        )
        gateway = LeadToolGateway(
            slot,
            notifications,
            allowed_operations=operations,
            lead_enrollment_ref=lead_enrollment_ref,
        )
        return LeadConnection(slot, notifications, gateway, output)
    except BaseException:
        if notifications is not None:
            transcript.unlisten(notifications._observe)
        output.close()
        raise
