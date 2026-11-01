"""Lead-only service tools; worker tools cannot dispatch or mint authority."""

from jsonschema import Draft202012Validator

from ai_task_manager.application.service.models import SCHEMAS as SERVICE_SCHEMAS
from ai_task_manager.domains.work.errors import AccessDenied

from .gateway import tool_schema


class LeadToolGateway:
    def __init__(self, slot, notifications, *, allowed_operations):
        if not set(allowed_operations) <= set(SERVICE_SCHEMAS):
            raise AccessDenied("Unknown lead service operation")
        if set(allowed_operations) & {"event_acknowledge", "session_resume"}:
            raise AccessDenied("Observation and session rotation use trusted controller routes")
        self.slot, self.notifications = slot, notifications
        self.generation = slot.generation
        self.schemas = {name: SERVICE_SCHEMAS[name] for name in allowed_operations}
        self.schemas["acknowledge_event"] = tool_schema("acknowledge_event")

    def call(self, name, arguments):
        if name not in self.schemas:
            raise AccessDenied("Tool is outside the configured lead surface")
        Draft202012Validator(self.schemas[name]).validate(arguments)
        if name != "acknowledge_event":
            return self.slot.call(self.generation, name, arguments)
        event = self.notifications.pending.get(arguments["event_id"])
        if event is None or event.challenge != arguments["challenge"]:
            raise AccessDenied("The lead has not received this event challenge")
        return self._acknowledge(arguments["event_id"], ("delivered", "ai_acknowledged"))

    def _acknowledge(self, event_id, stages):
        results = []
        for stage in stages:
            ref = self.notifications.reference(event_id, stage)
            result = self.slot.call(
                self.generation,
                "event_acknowledge",
                {
                    "event_id": event_id,
                    "stage": stage,
                    "observation_ref": ref,
                    "operation_key": f"host:{event_id}:{stage}",
                },
            )
            if not result.get("ok"):
                raise AccessDenied("Core has not accepted this scoped host receipt")
            results.append(result)
        return {"event_id": event_id, "receipts": results}

    def publish(self, event_id):
        """Controller-owned public sink path; no model-callable report issuer exists."""
        self.notifications.publish(event_id)
        return self._acknowledge(event_id, ("user_reported",))
