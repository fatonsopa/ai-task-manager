"""Pinned SDK protocol routing over our owned, environment-isolated process transport."""

import json

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
from ai_task_manager.supervisor.models import Command


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
        max_budget_usd=0.25,
        system_prompt="Work only through the managed task tools and follow current task authority.",
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
        return self.connected and not self.process._closed

    async def read_messages(self):
        try:
            while self.is_ready():
                frame = await anyio.to_thread.run_sync(self.transcript.read)
                if frame is not None:
                    yield frame
        except BaseException:
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

    async def initialize(self):
        await self.client.connect()
        # SDK initialization is not a native session or model-startup receipt.
        return await self.client.get_server_info()

    async def turn(self, text, *, input_id=None):
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
        async for message in self.client.receive_response():
            result = message
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
