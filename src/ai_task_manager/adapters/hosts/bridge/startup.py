"""Derive startup from owned protocol responses, never from a caller's readiness flag."""

from dataclasses import asdict

from ai_task_manager.adapters.hosts.startup import HostStartup
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope


class StartupObserver:
    def __init__(self, transcript, policy, *, expected_account, claude_auth_ref=None):
        self.transcript, self.policy = transcript, policy
        self.binding, self.journal = transcript.binding, transcript.journal
        self.expected_account, self.claude_auth_ref = expected_account, claude_auth_ref
        self.requests, self.responses = {}, {}
        self.ready_ref = None
        self.native_session, self.model = None, None
        self.model_source = None
        self.initialization_tool_calls = []
        self.admitted = False
        transcript.listen(self._observe)

    def expect(self, identity, method):
        if identity in self.requests or method not in {
            "initialize",
            "account/read",
            "thread/start",
        }:
            raise AccessDenied("Unexpected startup request identity")
        self.requests[identity] = method

    def _observe(self, frame, source):
        self.policy.check()
        if not self.admitted:
            if frame.get("method") == "item/tool/call":
                self.initialization_tool_calls.append(source)
            if frame.get("type") == "assistant" and any(
                block.get("type") == "tool_use"
                for block in frame.get("message", {}).get("content", [])
            ):
                self.initialization_tool_calls.append(source)
        if self.binding.host == "codex" and "id" in frame and "method" not in frame:
            method = self.requests.pop(frame["id"], None)
            if method is None:
                return
            if "error" in frame or not isinstance(frame.get("result"), dict):
                raise AccessDenied("Native startup request failed")
            self.responses[method] = (frame["result"], source)
            if method == "initialize":
                result = frame["result"]
                if result.get("codexHome") != self.policy.environment["CODEX_HOME"]:
                    raise AccessDenied("Codex initialized a different configuration home")
            elif method == "account/read":
                account = frame["result"].get("account")
                if (
                    not isinstance(account, dict)
                    or account.get("type") != "chatgpt"
                    or digest(account) != self.expected_account
                ):
                    raise AccessDenied(
                        "Actual native authentication is not the approved subscription"
                    )
            elif method == "thread/start":
                result = frame["result"]
                if (
                    set(self.responses) != {"initialize", "account/read", "thread/start"}
                    or result.get("modelProvider") != "openai"
                    or result.get("approvalPolicy") != "never"
                    or result.get("sandbox", {}).get("type") != "readOnly"
                    or result.get("cwd") != str(self.policy.cwd)
                    or not result.get("thread", {}).get("id")
                ):
                    raise AccessDenied("Codex did not confirm the restricted native startup")
                self.native_session = result["thread"]["id"]
                self.model = result.get("model") or None
                self.model_source = "thread/start.model" if self.model is not None else None
                self._ready(tuple(x[1] for x in self.responses.values()))
        elif self.binding.host == "codex" and frame.get("method") == "account/updated":
            if frame.get("params", {}).get("authMode") != "chatgpt":
                raise AccessDenied("Native authentication changed; stop and reconcile")
        elif (
            self.binding.host == "claude"
            and frame.get("type") == "system"
            and frame.get("subtype") == "init"
        ):
            if self.claude_auth_ref is None:
                raise AccessDenied("Claude needs an actual intended-account auth observation")
            auth = self.journal._read(self.claude_auth_ref, "trace")["payload"]
            if (
                auth.get("auth_method") != "claude.ai"
                or auth.get("account_hash") != self.expected_account
                or auth.get("configuration_digest") != self.binding.configuration_digest
                or frame.get("apiKeySource") != "none"
                or frame.get("cwd") != str(self.policy.cwd)
                or frame.get("claude_code_version") != "2.1.301"
                or not frame.get("session_id")
                or not frame.get("model")
            ):
                raise AccessDenied("Claude did not confirm the exact approved native startup")
            allowed = set(self.policy.allowed_tools)
            if set(frame.get("tools", [])) != allowed or any(
                server.get("status") != "connected" for server in frame.get("mcp_servers", [])
            ):
                raise AccessDenied("Claude reported unexpected or unavailable native tools")
            native_session = frame["session_id"]
            if self.native_session is not None and native_session != self.native_session:
                raise AccessDenied("Claude changed native session without managed rotation")
            self.native_session, self.model = native_session, frame["model"]
            self.model_source = "system.init.model"
            self._ready((self.claude_auth_ref, source))

    def _ready(self, sources):
        identity = self.transcript.process.verify_identity()
        if self.policy.configuration_digest != self.binding.configuration_digest:
            raise AccessDenied("Native configuration and attempt bindings differ")
        self.ready_ref = self.journal._append(
            "startup",
            {
                **asdict(self.binding),
                "process_id": identity.pid,
                "process_start_identity": identity.start_identity,
                "session_id": self.native_session,
                "model": self.model,
                "model_source": self.model_source,
            },
            sources=sources,
            expires_at=min(self.binding.deadline, self.journal.clock() + 60),
        )


class HostStartupReceipts:
    def __init__(self, journal, processes=None):
        self.journal = journal
        self.processes = processes if processes is not None else {}

    def verify_host_ready(self, opaque_ref):
        stored = self.journal._read(opaque_ref, "startup")
        data = stored["payload"]
        process = self.processes.get(data["attempt_id"])
        if process is None:
            raise AccessDenied("An active owned process resolver is required for host startup")
        identity = process.verify_identity()
        if (identity.pid, identity.start_identity) != (
            data["process_id"],
            data["process_start_identity"],
        ):
            raise AccessDenied("Native startup process identity changed")
        return HostStartup(
            Scope(**data["scope"]),
            data["attempt_id"],
            data["worker_id"],
            data["host"],
            data["process_id"],
            data["process_start_identity"],
            data["session_id"],
            data["principal_ref"],
            data["configuration_digest"],
            opaque_ref,
            stored["expires_at"],
        )
