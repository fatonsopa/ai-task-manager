import os
import sys
from dataclasses import replace
from pathlib import Path

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ai_task_manager.application.service.models import SCHEMAS
from tests.application.helpers import ServiceRig


def test_a22_actual_sdk_stdio_handshake_scoped_calls_and_errors(tmp_path):
    env = ServiceRig(tmp_path)
    own = env.rig.task(confirmed=False)
    foreign = env.rig.session(replace(env.rig.scope, logical_session="foreign"))
    other = env.rig.task(handle=foreign, confirmed=False)
    config = env.configuration()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_task_manager", "--config", str(config), "mcp", "serve"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        cwd=tmp_path,
    )

    async def check():
        async with Client(params, read_timeout_seconds=10) as client:
            assert client.server_info.name == "ai-task-manager"
            assert client.protocol_version
            listed = await client.list_tools()
            schemas = {tool.name: tool.input_schema for tool in listed.tools}
            assert schemas == SCHEMAS
            response = await client.call_tool("task_list", {})
            assert not response.is_error
            assert [row["id"] for row in response.structured_content["data"]] == [own.id]
            pending = await client.call_tool("task_show", {"task_id": own.id})
            assert pending.structured_content["data"]["sync_state"] == "pending"
            denied = await client.call_tool("task_show", {"task_id": other.id})
            assert denied.is_error and denied.structured_content["outcome"] == "denied"
            extra = await client.call_tool("task_list", {"human": True})
            assert extra.is_error
            unknown = await client.call_tool("mint_evidence", {"kind": "worker_start"})
            assert unknown.is_error

    anyio.run(check)


def test_a23_actual_sdk_concurrent_bounded_and_canceled_reads_do_not_ack(tmp_path):
    env = ServiceRig(tmp_path)
    own = env.rig.task(confirmed=False)
    event = env.rig.core.notifications.list_events(env.rig.handle)[-1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_task_manager", "--config", str(env.configuration()), "mcp", "serve"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        cwd=tmp_path,
    )

    async def check():
        async with Client(params, read_timeout_seconds=10) as client:
            responses = []

            async def read():
                response = await client.call_tool("task_list", {})
                responses.append(response.structured_content)

            async with anyio.create_task_group() as group:
                for _ in range(4):
                    group.start_soon(read)
            assert len(responses) == 4
            assert all(
                [row["id"] for row in response["data"]] == [own.id] for response in responses
            )
            oversized = await client.call_tool(
                "writing_review",
                {
                    "writing_id": "not-approved",
                    "review": {"text": "x" * 262145},
                },
            )
            assert (
                oversized.is_error and oversized.structured_content["outcome"] == "invalid_request"
            )
            with anyio.move_on_after(0.05) as cancellation:
                await client.call_tool(
                    "event_list", {"after": event.sequence, "timeout_seconds": 1}
                )
            assert cancellation.cancel_called
            after = await client.call_tool("task_show", {"task_id": own.id})
            assert (
                not after.is_error and after.structured_content["data"]["sync_state"] == "pending"
            )
        assert set(env.rig.core.notifications.receipts(env.rig.handle, event.id)) == {"queued"}
        assert env.rig.core.get_task(env.rig.handle, own.id) == own

    anyio.run(check)
