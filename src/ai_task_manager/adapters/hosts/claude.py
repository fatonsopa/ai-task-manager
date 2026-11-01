"""Restricted Claude command construction with an explicit account profile."""

from pathlib import Path

from ai_task_manager.supervisor.models import Command, RuntimeRefused

from .contracts import HostTurn, completed_process, records


def smoke_command(*, executable, home, cwd, settings, mcp_config, session_id, prompt):
    home = Path(home).resolve()
    profile = home / ".claude_roni"
    if not profile.is_dir() or profile.is_symlink():
        raise RuntimeRefused("The explicit claude_roni profile is unavailable")
    if not Path(settings).is_file() or not Path(mcp_config).is_file():
        raise RuntimeRefused("Explicit managed settings and MCP configuration are required")
    return Command(
        (
            str(executable),
            "--print",
            "--restricted",
            "--setting-sources",
            "",
            "--settings",
            str(settings),
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_config),
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--session-id",
            session_id,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-budget-usd",
            "0.25",
            prompt,
        ),
        Path(cwd),
        {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(profile),
        },
        60,
        "claude",
    )


def parse_turn(text, *, returncode, quiescent, expected_session=None):
    completed_process(returncode, quiescent)
    events = records(text)
    init, final, tools = None, None, []
    for event in events:
        if final is not None:
            raise RuntimeRefused("Events followed a terminal Claude result")
        if event["type"] == "system" and event.get("subtype") == "init":
            if init is not None or not event.get("session_id"):
                raise RuntimeRefused("Claude startup identity is missing or repeated")
            init = event
        elif init is None:
            raise RuntimeRefused("Claude events preceded startup identity")
        if init and event.get("session_id", init["session_id"]) != init["session_id"]:
            raise RuntimeRefused("Claude session identity changed")
        if event["type"] == "assistant":
            tools.extend(
                block["name"]
                for block in event.get("message", {}).get("content", [])
                if block.get("type") == "tool_use"
            )
        if event["type"] == "result":
            final = event
    if (
        not init
        or not final
        or final.get("is_error") is not False
        or final.get("subtype") != "success"
        or not isinstance(final.get("result"), str)
        or (expected_session is not None and init["session_id"] != expected_session)
    ):
        raise RuntimeRefused("Claude did not produce a matching successful terminal result")
    return HostTurn(
        "claude",
        init["session_id"],
        init.get("model"),
        final["result"],
        tuple(tools),
        final.get("usage", {}),
        final.get("total_cost_usd"),
    )
