"""Trusted composition and reload for an owned managed logical session."""

from threading import RLock

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .service import load_service


class ManagedServiceSlot:
    def __init__(self, loader, journal):
        self._loader, self.journal = loader, journal
        self._service = loader()
        self.scope, self.session_id = self._service.scope, self._service.session_id
        self.generation = max(
            [
                row["payload"]["generation"]
                for row in journal._history("rotation")
                if row["payload"].get("session_id") == self.session_id
            ],
            default=0,
        )
        self._lock = RLock()
        completed = {
            row["payload"]["operation_key"]
            for row in journal._history("rotation")
            if row["payload"].get("session_id") == self.session_id
        }
        self._fenced = any(
            identity.startswith("rotation:")
            and data.get("session_id") == self.session_id
            and identity.removeprefix("rotation:") not in completed
            for identity, data in journal._intents()
        )

    @classmethod
    def configured(cls, config_file, journal, observations, **trusted_factories):
        return cls(
            lambda: load_service(config_file, observations=observations, **trusted_factories),
            journal,
        )

    def call(self, generation, operation, data):
        with self._lock:
            if self._fenced or generation != self.generation:
                raise AccessDenied("Managed service was rotated or requires trusted recovery")
            if operation == "session_resume":
                return self.resume(
                    data["enrollment_ref"],
                    data["resume_ref"],
                    digest({"enrollment": data["enrollment_ref"], "resume": data["resume_ref"]}),
                )
            return self._service.call(operation, data)

    def resume(self, enrollment_ref, resume_ref, operation_key):
        with self._lock:
            if self._fenced:
                raise AccessDenied("Pending rotation must be recovered without a second resume")
            binding = {
                "scope": vars(self.scope),
                "session_id": self.session_id,
                "enrollment_ref": enrollment_ref,
                "resume_ref": resume_ref,
                "generation": self.generation,
            }
            if not self.journal.intend("rotation:" + operation_key, binding):
                raise AccessDenied(
                    "Existing rotation intent requires recovery, not a duplicate rotation"
                )
            self._fenced = True
            result = self._service.call(
                "session_resume", {"enrollment_ref": enrollment_ref, "resume_ref": resume_ref}
            )
            if not result.get("ok"):
                # No assumption that a failed publication rolled back a Core rotation.
                raise AccessDenied("Session rotation outcome is pending or failed")
            return self._reload(operation_key)

    def recover_reload(self, operation_key):
        """Reload a valid saved capability without performing another rotation.

        This is a trusted operator recovery operation, not a model-visible tool.
        """
        with self._lock:
            intent = self.journal.intent("rotation:" + operation_key)
            if intent is None or intent["session_id"] != self.session_id:
                raise AccessDenied("No matching owned rotation intent exists")
            self._fenced = True
            return self._reload(operation_key)

    def _reload(self, operation_key):
        replacement = self._loader()
        if replacement.scope != self.scope or replacement.session_id != self.session_id:
            raise AccessDenied("Reload cannot replace another logical session")
        generation = self.generation + 1
        self.journal._append(
            "rotation",
            {
                "operation_key": operation_key,
                "generation": generation,
                "session_id": self.session_id,
            },
        )
        self._service = replacement
        self.generation = generation
        self._fenced = False
        return {"session_id": self.session_id, "generation": self.generation, "reloaded": True}
