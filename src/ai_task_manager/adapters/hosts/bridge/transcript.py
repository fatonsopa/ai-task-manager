"""Only frames read from the owned transport become native-source observations."""

from ai_task_manager.config.files import digest

from .codex_settings import CONFIG_METHODS, project_response

SENSITIVE = frozenset(
    {
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "id_token",
        "openai_api_key",
    }
)


def redacted(value):
    if isinstance(value, dict):
        return {
            k: "[redacted]" if k.lower() in SENSITIVE else redacted(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


class OwnedTranscript:
    def __init__(self, process, journal, binding):
        self.process, self.journal, self.binding = process, journal, binding
        self._listeners = []
        self._last = None
        self._response_methods = {}

    def expect_response(self, identity, method):
        self._response_methods[identity] = method

    def listen(self, listener):
        self._listeners.append(listener)

    def read(self, timeout=0.2):
        self.process.verify_identity()
        frame = self.process.read(timeout)
        if frame is None:
            return None
        method = (
            self._response_methods.pop(frame.get("id"), None) if "method" not in frame else None
        )
        projected = method in CONFIG_METHODS or (
            self.binding.host == "codex"
            and "id" in frame
            and "method" not in frame
            and method is None
        )
        safe_frame = project_response(method, frame) if projected else redacted(frame)
        source = self.journal._append(
            "trace",
            {
                "attempt_id": self.binding.attempt_id,
                "worker_id": self.binding.worker_id,
                "configuration_digest": self.binding.configuration_digest,
                "process": vars(self.process.identity),
                "frame_hash": digest(safe_frame) if projected else digest(frame),
                "raw_frame_hash": digest(frame),
                "frame_kind": "configuration_projection" if projected else "native_frame",
                "frame": safe_frame,
            },
        )
        self._last = source
        for listener in self._listeners:
            listener(frame, source)
        return frame
