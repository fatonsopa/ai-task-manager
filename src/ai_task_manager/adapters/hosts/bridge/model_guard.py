"""A native model fence is sticky and scoped to the complete owned trace."""

from contextlib import contextmanager
from dataclasses import asdict
from threading import RLock

from ai_task_manager.domains.work.errors import AccessDenied

from .codex_history import binding_sources, replay_profile
from .codex_profile import CodexProfile, ProfileViolation


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
        result.append(row)
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
    rows = _owned_history(journal, owner, identity)
    if owner["host"] == "codex":
        try:
            state = replay_profile(
                rows,
                owner.get("requested_model"),
                owner.get("requested_effort"),
                owner.get("cwd"),
            )
        except ProfileViolation as error:
            raise AccessDenied("Owned Codex history failed its frozen profile") from error
        if (
            state.thread_id is None
            or state.thread_id != owner.get("session_id")
            or state.model != owner.get("model")
            or state.configured_effort != owner.get("native_configuration_effort")
            or owner.get("provider_internal_model") is not None
            or owner.get("observed_effort") is not None
        ):
            raise AccessDenied("Codex startup lacks its exact source-bound profile")
        return
    observed = None
    for row in rows:
        payload = row["payload"]
        if payload.get("effect") == "model_stability_refused":
            raise AccessDenied("Owned model history contains an unresolved refusal fence")
        frame = payload.get("frame")
        if not isinstance(frame, dict):
            continue
        observed, error = _model_event(frame, owner["host"], owner.get("requested_model"), observed)
        if error:
            raise AccessDenied("Owned model history failed its frozen profile")
    if owner["host"] == "claude" and observed != owner["requested_model"]:
        raise AccessDenied("The requested model has no matching native startup observation")


class PinnedModelGuard:
    def __init__(self, transcript, policy, *, configuration=None):
        self.transcript, self.journal, self.binding = (
            transcript,
            transcript.journal,
            transcript.binding,
        )
        self.requested = getattr(policy, "model", None)
        self.requested_effort = getattr(policy, "effort", None)
        self.cwd = str(policy.cwd)
        self.configuration = configuration
        if self.binding.host == "claude" and not self.requested:
            raise AccessDenied("Claude requires an explicitly configured model")
        self.observed, self.refusal = None, None
        self.codex = (
            CodexProfile(self.requested, self.requested_effort, self.cwd)
            if self.binding.host == "codex"
            else None
        )
        self._lock = RLock()
        self._restore(self._history())

    def configuration_changed(self, source):
        with self._lock:
            self._refuse("native_configuration_continuity_failed", source, record=True)

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
        if self.codex is not None:
            try:
                state = replay_profile(rows, self.requested, self.requested_effort, self.cwd)
            except ProfileViolation as error:
                self._refuse(error.reason, None, record=False)
            self.codex, self.observed = state, state.model
            return
        observed = None
        for row in rows:
            payload, source = row["payload"], row["ref"]
            if payload.get("effect") == "model_stability_refused":
                self._refuse(payload["reason"], source, record=False)
            frame = payload.get("frame")
            if not isinstance(frame, dict):
                continue
            observed, error = _model_event(frame, self.binding.host, self.requested, observed)
            if error:
                self._refuse(error, source, record=False)
        if observed is not None:
            self.observed = observed

    def observe(self, frame, source):
        with self._lock:
            if self.refusal:
                raise AccessDenied("The owned model refusal fence remains active")
            if self.codex is not None:
                try:
                    self.codex.observe(frame)
                except ProfileViolation as error:
                    self._refuse(error.reason, source, record=True)
                return
            observed, error = _model_event(frame, self.binding.host, self.requested, self.observed)
            if error:
                self._refuse(error, source, record=True)
            self.observed = observed

    def bind_codex(self, result, source, request_id, request_ref=None):
        """Controller-only correlated binding; no model-facing readiness issuer."""
        rows = self._history()
        with self._lock:
            if self.codex is None or self.refusal:
                raise AccessDenied("Codex profile binding is unavailable or refused")
            try:
                binding_sources(rows, result, source, request_ref, request_id)
                state = replay_profile(rows, self.requested, self.requested_effort, self.cwd)
                state.bind(result)
            except ProfileViolation as error:
                self._refuse(error.reason, source, record=True)
            self.journal._append(
                "trace",
                {
                    "effect": "codex_profile_bound",
                    "attempt_id": self.binding.attempt_id,
                    "worker_id": self.binding.worker_id,
                    "configuration_digest": self.binding.configuration_digest,
                    "process": vars(self.transcript.process.verify_identity()),
                    "request_id": request_id,
                    "request_ref": request_ref,
                    "response_ref": source,
                },
                sources=(request_ref, source),
            )
            self.codex, self.observed = state, state.model

    def _check(self, rows):
        self._restore(rows)
        if self.codex is not None and self.codex.thread_id is None:
            raise AccessDenied("No correlated native Codex thread is ready for managed effects")
        if self.binding.host == "claude" and self.observed != self.requested:
            raise AccessDenied("No matching observed model is ready for managed effects")

    def check(self):
        if self.configuration is not None:
            self.configuration.check()
        rows = self._history()
        with self._lock:
            self._check(rows)

    @contextmanager
    def effect_boundary(self):
        # Read durable history before taking the local state lock and before the
        # caller enters Core. No store/network/model wait occurs inside its yield.
        if self.configuration is not None:
            self.configuration.check()
        rows = self._history()
        with self._lock:
            self._check(rows)
            yield
