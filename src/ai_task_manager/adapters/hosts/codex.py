"""Isolated Codex exec plans; native-tool enforcement needs installed evidence."""

from pathlib import Path

from ai_task_manager.supervisor.models import Command, RuntimeRefused

from .contracts import HostTurn, completed_process, records


def smoke_command(*, executable, home, codex_home, cwd, prompt):
    auth_home = Path(codex_home).resolve()
    if not auth_home.is_dir():
        raise RuntimeRefused("An explicit intended Codex auth location is required")
    return Command(
        (
            str(executable),
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            prompt,
        ),
        Path(cwd),
        {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path(home).resolve()),
            "CODEX_HOME": str(auth_home),
        },
        60,
        "codex",
    )


def parse_turn(text, *, returncode, quiescent, expected_session=None):
    completed_process(returncode, quiescent)
    events = records(text)
    session, started, terminal, final, usage, tools = None, False, False, None, {}, []
    for event in events:
        kind = event["type"]
        if terminal:
            raise RuntimeRefused("Events followed a terminal Codex turn")
        if kind == "thread.started":
            if session is not None or not event.get("thread_id"):
                raise RuntimeRefused("Codex thread identity is missing or repeated")
            session = event["thread_id"]
        elif session is None:
            raise RuntimeRefused("Codex events preceded thread identity")
        if kind == "turn.started":
            if started:
                raise RuntimeRefused("Unexpected additional Codex turn")
            started = True
        if kind in {"error", "turn.failed"}:
            raise RuntimeRefused("Codex reported a failed turn")
        if kind.startswith("item."):
            if not started:
                raise RuntimeRefused("Codex item preceded turn startup")
            item = event.get("item", {})
            if item.get("type") == "agent_message" and kind == "item.completed":
                final = item.get("text")
            elif item.get("type") in {
                "command_execution",
                "mcp_tool_call",
                "web_search",
                "file_change",
            }:
                tools.append(item["type"])
        if kind == "turn.completed":
            terminal = True
            usage = event.get("usage", {})
    if not (session and started and terminal and isinstance(final, str)):
        raise RuntimeRefused("Codex did not produce a complete model turn")
    if expected_session is not None and session != expected_session:
        raise RuntimeRefused("Codex thread does not match its expected identity")
    return HostTurn("codex", session, None, final, tuple(tools), usage, None)
