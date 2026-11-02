"""A native model fence is sticky and scoped to the complete owned trace."""

from contextlib import contextmanager
from dataclasses import asdict
from threading import RLock

from ai_task_manager.domains.work.errors import AccessDenied


def _owned_history(journal, owner, identity=None):
    result = []
    for row in journal._history("trace"):
        payload = row["payload"]
        if not all(
            payload.get(name) == owner[name]
            for name in ("attempt_id", "worker_id", "configuration_digest")
        ):
            continue
        if identity is not None and payload.get("process") != identity:
            continue
        if payload.get("effect") == "model_stability_refused":
            result.append((None, row["ref"], payload["reason"]))
        elif isinstance(payload.get("frame"), dict):
            result.append((payload["frame"], row["ref"], None))
    return result


def _model_event(frame, host, requested, observed):
    if host != "claude":
        return observed, None
    if frame.get("type") == "system" and frame.get("subtype") == "model_refusal_fallback":
        return observed, "native_model_refusal_fallback"
    if frame.get("type") == "system" and frame.get("subtype") == "init":
        model = frame.get("model")
    elif frame.get("type") == "assistant":
        message = frame.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if observed is None:
            return observed, "assistant_before_bound_model_startup"
    else:
        return observed, None
    if not isinstance(model, str) or not model:
        return observed, "native_model_unknown"
    if model != requested or (observed is not None and model != observed):
        return observed, "native_model_changed"
    return model, None


def verify_model_history(journal, owner, identity):
    """An old valid startup receipt cannot hide later owned refusal/change frames."""
    if owner["host"] == "claude" and not owner.get("requested_model"):
        raise AccessDenied("Claude startup must bind an explicitly configured model")
    observed = None
    for frame, _, fence in _owned_history(journal, owner, identity):
        if fence:
            raise AccessDenied("Owned model history contains an unresolved refusal fence")
        observed, error = _model_event(frame, owner["host"], owner.get("requested_model"), observed)
        if error:
            raise AccessDenied("Owned model history failed its frozen profile")
    if owner["host"] == "claude" and observed != owner["requested_model"]:
        raise AccessDenied("The requested model has no matching native startup observation")


class PinnedModelGuard:
    def __init__(self, transcript, policy):
        self.transcript, self.journal, self.binding = (
            transcript,
            transcript.journal,
            transcript.binding,
        )
        self.requested = getattr(policy, "model", None)
        if self.binding.host == "claude" and not self.requested:
            raise AccessDenied("Claude requires an explicitly configured model")
        self.observed, self.refusal = None, None
        self._lock = RLock()
        self._restore(self._history())

    def _history(self):
        identity = self.transcript.process.identity
        return _owned_history(
            self.journal, asdict(self.binding), vars(identity) if identity is not None else None
        )

    def _refuse(self, reason, source, *, record):
        if self.refusal is None:
            self.refusal = reason
            if record:
                identity = self.transcript.process.identity
                self.journal._append(
                    "trace",
                    {
                        "effect": "model_stability_refused",
                        "reason": reason,
                        "attempt_id": self.binding.attempt_id,
                        "worker_id": self.binding.worker_id,
                        "configuration_digest": self.binding.configuration_digest,
                        "process": vars(identity) if identity is not None else None,
                        "requested_model": self.requested,
                        "initial_model": self.observed,
                    },
                    sources=(source,),
                )
        raise AccessDenied("Native model stability failed; the attempt requires review")

    def _restore(self, rows):
        if self.refusal:
            raise AccessDenied("The owned model refusal fence remains active")
        observed = None
        for frame, source, fence in rows:
            if fence:
                self._refuse(fence, source, record=False)
            observed, error = _model_event(frame, self.binding.host, self.requested, observed)
            if error:
                self._refuse(error, source, record=False)
        if observed is not None:
            self.observed = observed

    def observe(self, frame, source):
        with self._lock:
            if self.refusal:
                raise AccessDenied("The owned model refusal fence remains active")
            observed, error = _model_event(frame, self.binding.host, self.requested, self.observed)
            if error:
                self._refuse(error, source, record=True)
            self.observed = observed

    def native_ready(self, model, source):
        """Bind reported Codex startup metadata without inventing unknown identity."""
        with self._lock:
            if self.requested is not None and model != self.requested:
                self._refuse("native_model_changed", source, record=True)
            if self.observed is not None and model != self.observed:
                self._refuse("native_model_changed", source, record=True)
            self.observed = model

    def _check(self, rows):
        self._restore(rows)
        if self.binding.host == "claude" and self.observed != self.requested:
            raise AccessDenied("No matching observed model is ready for managed effects")

    def check(self):
        rows = self._history()
        with self._lock:
            self._check(rows)

    @contextmanager
    def effect_boundary(self):
        # Read durable history before taking the local state lock and before the
        # caller enters Core. No store/network/model wait occurs inside its yield.
        rows = self._history()
        with self._lock:
            self._check(rows)
            yield
