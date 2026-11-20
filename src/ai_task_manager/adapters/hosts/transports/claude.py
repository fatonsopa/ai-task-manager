"""Pinned SDK protocol routing over our owned, environment-isolated process transport."""

import json
from time import monotonic as diagnostic_clock

import anyio
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkMcpTool,
    create_sdk_mcp_server,
)
from claude_agent_sdk._internal.transport import Transport
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.observability.failures import FailureStage, capture_failure
from ai_task_manager.supervisor.models import Command


def terminal_observation(result, expected_session):
    """Only exact SDK result fields with closed labels/numeric usage are retained."""
    data = {
        "result_present": type(result) is ResultMessage,
        "subtype": None,
        "is_error": None,
        "session_matches": None,
        "num_turns": None,
        "usage": None,
        "total_cost_usd": None,
        "error_count": None,
        "terminal_reason": None,
    }
    if type(result) is not ResultMessage:
        return data
    fields = object.__getattribute__(result, "__dict__")
    if type(fields) is not dict:
        return data
    subtype = fields.get("subtype")
    data["subtype"] = (
        subtype
        if type(subtype) is str
        and subtype
        in {
            "success",
            "error_max_turns",
            "error_max_budget_usd",
            "error_during_execution",
            "error_during_validation",
        }
        else "unrecognized"
    )
    data["is_error"] = fields.get("is_error") if type(fields.get("is_error")) is bool else None
    session = fields.get("session_id")
    if type(session) is str and type(expected_session) is str:
        data["session_matches"] = session == expected_session
    for key in ("num_turns", "duration_ms", "duration_api_ms", "api_error_status"):
        value = fields.get(key)
        data[key] = value if type(value) is int and 0 <= value <= 2**53 else None
    cost = fields.get("total_cost_usd")
    if type(cost) in {float, int} and 0 <= cost <= 1_000_000:
        data["total_cost_usd"] = cost
    usage = fields.get("usage")
    if type(usage) is dict:
        data["usage"] = {
            key: usage[key]
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
            if key in usage and type(usage[key]) is int and 0 <= usage[key] <= 2**53
        }
    errors = fields.get("errors")
    data["error_count"] = len(errors) if type(errors) is list else None
    reason = fields.get("terminal_reason")
    if type(reason) is str:
        data["terminal_reason"] = (
            reason
            if reason in {"completed", "max_turns", "aborted_streaming", "aborted_tools"}
            else "unrecognized"
        )
    return data


def _record_failure(owner, source, error, stage, started, terminal=None):
    if len(owner.failure_diagnostics) >= 8:
        owner.diagnostics_truncated = True
        return
    item = {"failure": capture_failure(error, stage, started=started), "retained": False}
    owner.failure_diagnostics.append(item)
    try:
        fields = object.__getattribute__(source, "__dict__")
        journal, binding = fields["journal"], fields["binding"]
        item["reference"] = journal._append(
            "trace",
            {
                "effect": "claude_failure_cause",
                "attempt_id": binding.attempt_id,
                "failure": item["failure"],
                "terminal": terminal,
            },
        )
        item["retained"] = True
    except BaseException as retention_error:
        item["retention_failure"] = capture_failure(
            retention_error, FailureStage.DIAGNOSTIC_RETENTION
        )


def sdk_options(policy, gateway):
    tools = []
    for name, schema in gateway.schemas.items():

        async def handler(args, operation=name):
            try:
                value = gateway.call(operation, args)
                return {"content": [{"type": "text", "text": json.dumps(value)}]}
            except Exception as exc:
                return {"content": [{"type": "text", "text": type(exc).__name__}], "isError": True}

        tools.append(SdkMcpTool(name, "Use the assigned managed task operation.", schema, handler))
    server = create_sdk_mcp_server("aitm", tools=tools)
    return ClaudeAgentOptions(
        cli_path=str(policy.executable),
        cwd=str(policy.cwd),
        tools=[],
        allowed_tools=list(policy.allowed_tools),
        permission_mode="dontAsk",
        mcp_servers={"aitm": server},
        setting_sources=[],
        strict_mcp_config=True,
        include_hook_events=True,
        model=policy.model,
        effort=policy.effort,
        max_turns=8,
        max_budget_usd=0.25,
        max_buffer_size=262144,
        system_prompt=(
            "Work only through the managed task tools and follow current task authority."
            + (" Use create_file for new files and write_file with the observed hash for updates."
               if "create_file" in gateway.schemas else "")
        ),
        extra_args={
            "restricted": None,
            "print": None,
            "replay-user-messages": None,
            "no-session-persistence": None,
        },
    )


def sdk_command(policy, options, timeout):
    policy.check()
    if (
        options.cli_path != str(policy.executable)
        or options.tools != []
        or options.setting_sources != []
        or not options.strict_mcp_config
        or options.permission_mode != "dontAsk"
        or options.model != policy.model
        or options.effort != policy.effort
        or options.fallback_model is not None
        or options.max_turns != 8
        or options.max_budget_usd != 0.25
        or options.max_buffer_size != 262144
        or set(options.mcp_servers) != {"aitm"}
        or set(options.allowed_tools) != set(policy.allowed_tools)
        or options.extra_args
        != {
            "restricted": None,
            "print": None,
            "replay-user-messages": None,
            "no-session-persistence": None,
        }
    ):
        raise AccessDenied("Claude options do not match the supported managed tool surface")
    # Only the pinned SDK's argv builder is reused. Its connect(), version probe and
    # environment-merging subprocess implementation are never invoked.
    builder = SubprocessCLITransport(prompt="", options=options)
    argv = tuple(builder._build_command())
    return Command(argv, policy.cwd, dict(policy.environment), timeout, "claude")


class ClaudeTransport(Transport):
    def __init__(self, process, transcript):
        self.process, self.transcript = process, transcript
        self.connected = False
        self.failure_diagnostics = []
        self.diagnostics_truncated = False

    async def connect(self):
        if self.connected:
            return
        await anyio.to_thread.run_sync(self.process.start)
        self.connected = True

    async def write(self, data):
        await anyio.to_thread.run_sync(self.process.write, data)

    async def end_input(self):
        await anyio.to_thread.run_sync(self.process.end_input)

    def is_ready(self):
        return self.connected and not self.process._closed and not self.process.stopping

    async def read_messages(self):
        started = diagnostic_clock()
        try:
            while self.is_ready():
                frame = await anyio.to_thread.run_sync(self.transcript.read)
                if frame is not None:
                    yield frame
        except BaseException as error:
            if self.process.stopping:
                return
            _record_failure(
                self,
                self.transcript,
                error,
                FailureStage.NATIVE_TRANSPORT,
                started,
            )
            await anyio.to_thread.run_sync(self.process.stop)
            raise

    async def close(self):
        if self.connected and not self.process._closed:
            await anyio.to_thread.run_sync(self.process.stop)
        self.connected = False


class ClaudeSession:
    def __init__(self, options, transport, startup):
        self.client = ClaudeSDKClient(options, transport=transport)
        self.transport, self.startup = transport, startup
        self.failure_diagnostics = []
        self.diagnostics_truncated = False

    async def initialize(self):
        await self.client.connect()
        # SDK initialization is not a native session or model-startup receipt.
        return await self.client.get_server_info()

    async def turn(self, text, *, input_id=None):
        started = diagnostic_clock()
        result, count = None, 0
        try:
            return await self._turn(text, input_id=input_id)
        except BaseException as error:
            # _turn stores only local SDK objects; no model/provider strings are copied.
            result, count = self._last_turn_observation
            try:
                terminal = terminal_observation(result, self.startup.native_session)
                terminal["messages_received"] = count
            except BaseException:
                terminal = {"result_present": None, "projection_failed": True}
            _record_failure(
                self,
                self.startup,
                error,
                FailureStage.NATIVE_TURN,
                started,
                terminal,
            )
            raise

    async def _turn(self, text, *, input_id=None):
        self._last_turn_observation = (None, 0)
        if input_id is None:
            await self.client.query(text)
        else:

            async def user_input():
                yield {
                    "type": "user",
                    "uuid": input_id,
                    "session_id": self.startup.native_session or "default",
                    "message": {"role": "user", "content": text},
                }

            await self.client.query(user_input())
        result = None
        count = 0
        async for message in self.client.receive_response():
            result = message
            count += 1
            self._last_turn_observation = (result, count)
        if (
            not isinstance(result, ResultMessage)
            or result.is_error is not False
            or result.subtype != "success"
            or not self.startup.native_session
            or result.session_id != self.startup.native_session
        ):
            raise AccessDenied("Claude did not complete a matching successful native turn")
        return result

    async def close(self):
        await self.client.disconnect()
