"""Model effect surface: exact registered tools with a current scoped lease."""

import math
from threading import RLock

from jsonschema import Draft202012Validator

from ai_task_manager.adapters.resources.paths import unchanged
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

SCHEMAS = {
    "read_file": {"path": {"type": "string"}},
    "write_file": {
        "path": {"type": "string"},
        "text": {"type": "string"},
        "expected_hash": {"type": ["string", "null"]},
        "operation_key": {"type": "string"},
    },
    "run_command": {"command_ref": {"type": "string"}, "operation_key": {"type": "string"}},
    "acknowledge_event": {"event_id": {"type": "string"}, "challenge": {"type": "string"}},
}


def tool_schema(name):
    fields = SCHEMAS[name]
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


class ManagedToolGateway:
    def __init__(
        self,
        core,
        handle,
        dispatch,
        workspace,
        resources,
        journal,
        *,
        deadline,
        commands=None,
        notifications=None,
        process_registry=None,
    ):
        if not math.isfinite(deadline):
            raise AccessDenied("A finite immutable work deadline is required")
        self.core, self.handle, self.dispatch = core, handle, dispatch
        self.workspace, self.resources, self.journal = workspace, resources, journal
        self.deadline, self.commands, self.notifications = deadline, commands, notifications
        self.process_registry = process_registry
        self.schemas = {name: tool_schema(name) for name in SCHEMAS}
        self.lock = RLock()
        self._attempt_version = None

    def admit(self):
        attempt = self.core.get_attempt(self.handle, self.dispatch.attempt_id)
        if attempt.host in {"claude", "codex"} and self.process_registry is None:
            raise AccessDenied("Model workers require registered actual native process ownership")
        self.core.execution.validate_work_lease(self.handle, attempt.id, attempt.version)
        self._attempt_version = attempt.version
        try:
            self.check()
        except BaseException:
            self._attempt_version = None
            raise

    def check(self):
        if self._attempt_version is None or self.journal.clock() >= self.deadline:
            raise AccessDenied("No current admitted work lease or original deadline expired")
        if self.process_registry is not None:
            process = self.process_registry.get(self.dispatch.attempt_id)
            if process is None:
                raise AccessDenied("Registered native process ownership is unavailable")
            process.verify_identity()
        self._resource_fact = self.resources.verify(self.dispatch.resources)
        return self.core.execution.validate_work_lease(
            self.handle,
            self.dispatch.attempt_id,
            self._attempt_version,
        )

    def call(self, name, arguments):
        if name not in SCHEMAS:
            raise AccessDenied("Native or unknown tools cannot bypass the managed task surface")
        Draft202012Validator(tool_schema(name)).validate(arguments)
        with self.lock:
            self.check()
            if name == "acknowledge_event":
                if self.notifications is None:
                    raise AccessDenied("Host notification observer is unavailable")
                # The actual native request was already observed by the bound transcript.
                return {
                    "observation_ref": self.notifications.reference(
                        arguments["event_id"], "ai_acknowledged"
                    )
                }
            if name == "run_command":
                if self.commands is None:
                    raise AccessDenied("No reviewed confined command registry is configured")
                return self.commands.execute_worker_command(
                    self.handle,
                    self.dispatch.attempt_id,
                    arguments["command_ref"],
                    arguments["operation_key"],
                )
            intent = None
            if name == "write_file":
                intent = "edit:" + arguments["operation_key"]
                binding = {
                    "attempt_id": self.dispatch.attempt_id,
                    "arguments_hash": digest(arguments),
                    "deadline": self.deadline,
                }
                if not self.journal.intend(intent, binding):
                    # A prior intent without atomic filesystem receipt is uncertain, not retry-safe.
                    raise AccessDenied("Existing edit intent requires reconciliation")
            # Core serializes this bounded publication with independent human writers.
            with self.core.execution.work_boundary(
                self.handle,
                self.dispatch.attempt_id,
                self._attempt_version,
            ):
                self.check_deadline_resources()
                if name == "read_file":
                    return self.workspace.read(arguments["path"])
                result = self.workspace.write(
                    arguments["path"], arguments["text"], arguments["expected_hash"]
                )
            self.journal._append(
                "trace", {"effect": "file_published", "intent": intent, "result": result}
            )
            return result

    def check_deadline_resources(self):
        if self.journal.clock() >= self.deadline:
            raise AccessDenied("Original managed-work deadline expired")
        # No Git subprocess, provider or host call while holding the Core effect lock.
        for name, path in (
            ("worktree", self.dispatch.resources.worktree),
            ("database", self.dispatch.resources.database),
        ):
            unchanged(path, self._resource_fact[name]["physical"])
