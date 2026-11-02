"""Pinned Codex notification decoding; configured identity is not execution telemetry."""


class ProfileViolation(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def required_text(value, reason):
    if not isinstance(value, str) or not value:
        raise ProfileViolation(reason)
    return value


def restricted_execution(settings, cwd, sandbox_key):
    sandbox = settings.get(sandbox_key)
    if (
        settings.get("modelProvider") != "openai"
        or settings.get("cwd") != cwd
        or settings.get("approvalPolicy") != "never"
        or settings.get("approvalsReviewer") != "user"
        or not isinstance(sandbox, dict)
        or sandbox.get("type") != "readOnly"
        or sandbox.get("networkAccess", False) is not False
        or set(sandbox) - {"type", "networkAccess"}
    ):
        raise ProfileViolation("native_execution_configuration_changed_or_unknown")


class CodexProfile:
    """One correlated thread and its exact configured model/effort, with no I/O."""

    METHODS = frozenset(
        {
            "model/rerouted",
            "thread/settings/updated",
            "model/safetyBuffering/updated",
            "model/verification",
        }
    )

    def __init__(self, requested_model, requested_effort, cwd):
        self.requested_model, self.requested_effort, self.cwd = (
            requested_model,
            requested_effort,
            cwd,
        )
        self.thread_id = self.model = self.configured_effort = None
        self.pending = []

    def bind(self, result):
        if self.thread_id is not None:
            raise ProfileViolation("native_thread_rebinding_requires_managed_rotation")
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise ProfileViolation("native_thread_unknown")
        thread_id = required_text(thread.get("id"), "native_thread_unknown")
        model = required_text(result.get("model"), "native_model_unknown")
        effort = result.get("reasoningEffort")
        if effort is not None:
            required_text(effort, "native_configured_effort_unknown")
        if (
            self.requested_model is not None
            and model != self.requested_model
            or self.requested_effort is not None
            and effort != self.requested_effort
        ):
            raise ProfileViolation("native_requested_profile_not_acknowledged")
        restricted_execution(result, self.cwd, "sandbox")
        self.thread_id, self.model, self.configured_effort = thread_id, model, effort
        for frame in self.pending:
            self.observe(frame)
        self.pending = []

    def _settings(self, settings):
        if not isinstance(settings, dict):
            raise ProfileViolation("native_thread_settings_unknown")
        restricted_execution(settings, self.cwd, "sandboxPolicy")
        expected_model = self.model if self.thread_id else self.requested_model
        expected_effort = self.configured_effort if self.thread_id else self.requested_effort
        model = required_text(settings.get("model"), "native_model_unknown")
        effort = settings.get("effort")
        if effort is not None:
            required_text(effort, "native_configured_effort_unknown")
        collaboration = settings.get("collaborationMode")
        mode_settings = collaboration.get("settings") if isinstance(collaboration, dict) else None
        if (
            expected_model is not None
            and model != expected_model
            or (self.thread_id is not None or expected_effort is not None)
            and effort != expected_effort
            or not isinstance(mode_settings, dict)
            or collaboration.get("mode") != "default"
            or mode_settings.get("model") != model
            or mode_settings.get("reasoning_effort") != effort
        ):
            raise ProfileViolation("native_model_or_configured_effort_changed")

    def observe(self, frame):
        method = frame.get("method")
        if not isinstance(method, str) or method not in self.METHODS:
            return
        params = frame.get("params")
        if not isinstance(params, dict):
            raise ProfileViolation("native_profile_notification_malformed")
        thread = required_text(params.get("threadId"), "native_profile_thread_unknown")
        if self.thread_id is not None and thread != self.thread_id:
            raise ProfileViolation("native_profile_thread_scope_ambiguous")
        if method != "thread/settings/updated":
            required_text(params.get("turnId"), "native_profile_turn_unknown")
        if method == "model/rerouted":
            # A provider safety decision is never permission to select its fallback.
            raise ProfileViolation("native_model_rerouted")
        if method == "model/verification":
            raise ProfileViolation("native_verification_requires_operator_review")
        if method == "thread/settings/updated":
            self._settings(params.get("threadSettings"))
        else:
            model = required_text(params.get("model"), "native_model_unknown")
            if (
                type(params.get("showBufferingUi")) is not bool
                or any(
                    not isinstance(params.get(field), list)
                    or not all(isinstance(value, str) for value in params[field])
                    for field in ("useCases", "reasons")
                )
                or params.get("fasterModel") is not None
                and not isinstance(params["fasterModel"], str)
            ):
                raise ProfileViolation("native_buffering_notification_malformed")
            expected = self.model if self.thread_id else self.requested_model
            if expected is not None and model != expected:
                raise ProfileViolation("native_buffering_model_changed")
            # fasterModel is only an offered alternative. It never binds identity.
        if self.thread_id is None:
            if len(self.pending) >= 64:
                raise ProfileViolation("native_profile_pending_inventory_exceeded")
            self.pending.append(frame)
