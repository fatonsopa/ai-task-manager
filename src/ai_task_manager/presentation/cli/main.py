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


def main(argv=None, *, workflow_registry=None):
    parser = argparse.ArgumentParser(prog="aitm")
    parser.add_argument("--config", default=os.environ.get("AITM_CONFIG_FILE"))
    parser.add_argument("--session-file")
    parser.add_argument("--installation")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--ascii", action="store_true")
    parser.add_argument("--refresh-seconds", type=float, default=1)
    parser.add_argument("--layout-context")
    parser.add_argument(
        "group",
        choices=[
            "session",
            "writing",
            "task",
            "worker",
            "event",
            "sync",
            "mcp",
            "view",
            "workflow",
        ],
    )
    parser.add_argument("action", nargs="?")
    parser.add_argument("--input", help="JSON input file; use - for stdin")
    args = parser.parse_args(argv)
    try:
        if not args.config:
            raise ValueError("A sealed configuration is required; cwd is not authority")
        if args.group == "workflow" and args.action == "owner":
            if not args.installation or args.input or args.session_file:
                raise ValueError("Owner launch needs installation and no request/session override")
            from ai_task_manager.installation.workflows import launch

            if workflow_registry is None:
                from ai_task_manager.installation.native import resolve

                workflow_registry = resolve(args.config, args.installation)
            envelope = launch(args.config, args.installation, registry=workflow_registry)
            sys.stdout.write(json.dumps(envelope, ensure_ascii=False, allow_nan=False) + "\n")
            return exit_code(envelope)
        if args.installation:
            raise ValueError("Installation input is only valid for workflow owner")
        if args.group == "sync" and args.action == "listen":
            if args.input:
                raise ValueError("Listener uses only sealed operator configuration")
            from ai_task_manager.application.linear_sync.listener import serve_config

            def emit(event):
                sys.stdout.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                sys.stdout.flush()

            return serve_config(args.config, args.session_file, emit)
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
                layout_context=args.layout_context,
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
