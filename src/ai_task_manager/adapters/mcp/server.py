"""Use the official MCP SDK for negotiation, framing and cancellation."""

import json

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from ai_task_manager.application.service.models import SCHEMAS, failure


def create_server(service):
    async def list_tools(context, params):
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    description="Guarded session operation: " + name.replace("_", " "),
                    input_schema=schema,
                )
                for name, schema in SCHEMAS.items()
            ]
        )

    async def call_tool(context, params):
        try:
            envelope = await anyio.to_thread.run_sync(
                service.call, params.name, params.arguments or {}
            )
        except Exception as exc:
            envelope = failure(exc)
        return CallToolResult(
            content=[
                TextContent(
                    type="text", text=json.dumps(envelope, ensure_ascii=False, allow_nan=False)
                )
            ],
            structured_content=envelope,
            is_error=not envelope["ok"],
        )

    return Server(
        "ai-task-manager",
        version="0.1.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=SCHEMAS.get,
        instructions=(
            "Use only this bound session. Pending state is not completed work; "
            "provide actual reviewed and observed proof references."
        ),
    )


async def serve(service):
    server = create_server(service)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
