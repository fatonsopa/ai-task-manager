"""Structured CLI over the same service used by MCP."""

import argparse
import json
import os
import sys

import anyio

from ai_task_manager.application.service.models import SCHEMAS, exit_code, failure
from ai_task_manager.config.files import MAX_JSON_BYTES, strict_json
from ai_task_manager.config.service import load_service

from .render import task_lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aitm")
    parser.add_argument("--config", default=os.environ.get("AITM_CONFIG_FILE"))
    parser.add_argument("--session-file")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--ascii", action="store_true")
    parser.add_argument("--refresh-seconds", type=float, default=1)
    parser.add_argument(
        "group", choices=["session", "writing", "task", "worker", "event", "sync", "mcp", "view"]
    )
    parser.add_argument("action", nargs="?")
    parser.add_argument("--input", help="JSON input file; use - for stdin")
    args = parser.parse_args(argv)
    try:
        if not args.config:
            raise ValueError("A sealed configuration is required; cwd is not authority")
        service = load_service(args.config, args.session_file)
        if args.group == "view":
            if args.input or args.json:
                raise ValueError(
                    "View uses only the configured session and accepts no input payload"
                )
            from ai_task_manager.presentation.terminal.commands import show

            return show(
                service,
                args.action,
                ascii=args.ascii,
                color=not args.no_color,
                refresh_seconds=args.refresh_seconds,
            )
        if args.group == "mcp":
            if args.action not in {None, "serve"} or args.input:
                raise ValueError("MCP accepts only the serve action")
            from ai_task_manager.adapters.mcp.server import serve

            anyio.run(serve, service)
            return 0
        operation = (
            "sync_now"
            if args.group == "sync" and args.action == "now"
            else args.group + "_" + (args.action or "show")
        )
        if operation not in SCHEMAS:
            raise ValueError("Unknown command")
        data = {}
        if args.input:
            if args.input == "-":
                raw = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
            else:
                with open(args.input, "rb") as stream:
                    raw = stream.read(MAX_JSON_BYTES + 1)
            data = strict_json(raw)
        envelope = service.call(operation, data)
    except Exception as exc:
        from ai_task_manager.domains.work.errors import ValidationError

        envelope = (
            failure(ValidationError(str(exc))) if isinstance(exc, ValueError) else failure(exc)
        )
    output = sys.stdout if envelope["ok"] else sys.stderr
    if not args.json and args.group == "task" and args.action == "list" and envelope["ok"]:
        output.write(
            task_lines(
                envelope["data"],
                color=output.isatty() and not args.no_color and "NO_COLOR" not in os.environ,
                quarantined_count=envelope.get("quarantined_count", 0),
            )
            + "\n"
        )
    else:
        output.write(json.dumps(envelope, ensure_ascii=False, allow_nan=False) + "\n")
    return exit_code(envelope)
