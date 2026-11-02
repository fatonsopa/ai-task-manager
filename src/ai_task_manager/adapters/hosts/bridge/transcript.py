"""Only frames read from the owned transport become native-source observations."""

from ai_task_manager.config.files import digest

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

    def listen(self, listener):
        self._listeners.append(listener)

    def read(self, timeout=0.2):
        self.process.verify_identity()
        frame = self.process.read(timeout)
        if frame is None:
            return None
        source = self.journal._append(
            "trace",
            {
                "attempt_id": self.binding.attempt_id,
                "worker_id": self.binding.worker_id,
                "configuration_digest": self.binding.configuration_digest,
                "process": vars(self.process.identity),
                "frame_hash": digest(frame),
                "frame": redacted(frame),
            },
        )
        self._last = source
        for listener in self._listeners:
            listener(frame, source)
        return frame
