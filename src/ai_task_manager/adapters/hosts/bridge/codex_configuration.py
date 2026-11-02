"""Positive native configuration stages; synthetic responses are not host conformance."""

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .codex_settings import RESTRICTIONS, project_response


class CodexConfigurationObserver:
    def __init__(self, settings, journal, binding, policy, conformance):
        self.settings, self.journal, self.binding, self.policy = settings, journal, binding, policy
        self.conformance = conformance
        self.observations, self.projections = {}, {}
        self.thread_id = None
        self.pages, self.cursors = 0, set()
        self.inventory_complete, self.fenced = False, False
        self.account_observed = False
        self.account_metadata = None
        self.account_source = None
        self.fence_callback = None
        self.fenced = any(
            row["payload"].get("effect") == "codex_configuration_fenced"
            and row["payload"].get("attempt_id") == binding.attempt_id
            and row["payload"].get("configuration_digest") == binding.configuration_digest
            for row in journal._history("trace")
        )

    def check(self):
        try:
            self.settings.check()
        except AccessDenied:
            self.fence()
            raise
        if (
            self.account_metadata is not None
            and self.settings.sources.credential_metadata() != self.account_metadata
        ):
            self.fence()
        if self.fenced:
            raise AccessDenied("Native configuration continuity needs trusted recovery")

    def observe_account(self, source):
        current = self.settings.sources.credential_metadata()
        if self.account_metadata is not None and current != self.account_metadata:
            self.fence()
            raise AccessDenied("Changed native cache requires a fresh trusted launch")
        self.account_metadata, self.account_source = current, source
        self.account_observed = True

    def accept(self, method, result, source, params):
        self.check()
        safe = project_response(method, {"result": result})["result"]
        if method == "config/read":
            expected = {
                **RESTRICTIONS,
                "forced_login_method": "chatgpt",
                "model_hash": digest(self.settings.model),
                "model_reasoning_effort": self.settings.effort,
            }
            if safe["effective"] != expected or not safe["origins_present"] or not safe["layers"]:
                raise AccessDenied("Effective native configuration is missing or incompatible")
            known = {digest(path) for path in self.settings.sources.sealed_paths}
            for layer in safe["layers"]:
                if layer.get("type") == "sessionFlags":
                    continue
                if layer.get("type") not in {"user", "system", "packagedDefaults", "project"}:
                    raise AccessDenied("Native managed source provenance is unobserved")
                path = layer.get("file_hash")
                if path not in known or not layer.get("version_present"):
                    raise AccessDenied("Native configuration layer was not source-sealed")
            if safe["servers"] != {name: False for name in self.settings.check()}:
                raise AccessDenied("Effective foreign MCP exclusion is unobserved")
        elif method == "configRequirements/read":
            req = safe["requirements"]
            if req is not None:
                if not isinstance(req, dict) or req["unsupported_keys"]:
                    raise AccessDenied("Managed requirements are unknown or unsupported")
                values = req["requirements"]
                for key, wanted in {
                    "allowedApprovalPolicies": "never",
                    "allowedSandboxModes": "readOnly",
                    "allowedWebSearchModes": "disabled",
                    "allowedApprovalsReviewers": "user",
                    "allowedLoginMethods": "chatgpt",
                }.items():
                    if key in values and wanted not in values[key]:
                        raise AccessDenied("Managed requirement contradicts the native profile")
                if values.get("modelProvider", "openai") != "openai" or any(
                    value is not False for value in values.get("featureRequirements", {}).values()
                ):
                    raise AccessDenied("Managed features or provider contradict exclusions")
        elif method == "mcpServerStatus/list":
            if params.get("threadId") != self.thread_id or params.get("detail") != "full":
                raise AccessDenied("MCP inventory belongs to another native thread")
            cursor = result.get("nextCursor")
            self.pages += 1
            if (
                self.pages > 32
                or not safe["cursor_present"]
                or safe["inventory_error"]
                or safe["entries"] != 0
                or cursor is not None
                and (not isinstance(cursor, str) or cursor in self.cursors)
            ):
                raise AccessDenied("Complete empty native MCP inventory is unconfirmed")
            if cursor is not None:
                self.cursors.add(cursor)
            self.inventory_complete = cursor is None
        else:
            raise AccessDenied("Unknown native configuration observation")
        self.observations[method], self.projections[method] = source, safe

    def pre_thread(self):
        self.check()
        if not self.account_observed or set(self.observations) != {
            "config/read",
            "configRequirements/read",
        }:
            raise AccessDenied("Actual effective native configuration must precede thread creation")
        # This installer/evaluator port must resolve actual pinned evidence. A
        # requested flag or authored response cannot replace it in normal use.
        verifier = getattr(self.conformance, "require_native_conformance", None)
        reference = verifier(self.policy, self.binding) if callable(verifier) else None
        if not isinstance(reference, str) or not reference:
            raise AccessDenied("Actual pinned native-tool conformance is unavailable")

    def thread(self, result):
        self.pre_thread()
        if "instructionSources" not in result or result["instructionSources"] != list(
            self.settings.instruction_sources
        ):
            raise AccessDenied("Actual loaded native instruction sources are unconfirmed")
        self.thread_id = result["thread"]["id"]

    def ready(self):
        self.check()
        if not self.thread_id or not self.inventory_complete:
            raise AccessDenied("Native thread inventory is incomplete")
        return self.journal._append(
            "trace",
            {
                "effect": "codex_configuration_ready",
                "attempt_id": self.binding.attempt_id,
                "configuration_digest": self.binding.configuration_digest,
                "source_fingerprint": self.settings.fingerprint,
                "native_thread": self.thread_id,
                "projection_digest": digest(self.projections),
            },
            sources=tuple(self.observations.values()),
        )

    def fence(self):
        if self.fenced:
            return
        self.fenced = True
        source = self.journal._append(
            "trace",
            {
                "effect": "codex_configuration_fenced",
                "attempt_id": self.binding.attempt_id,
                "configuration_digest": self.binding.configuration_digest,
            },
        )
        if self.fence_callback is not None:
            self.fence_callback(source)

    def account_changed(self):
        self.account_observed = False
        if self.thread_id is not None:
            self.fence()
