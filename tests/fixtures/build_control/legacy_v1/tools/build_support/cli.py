"""Argument parsing and JSON presentation for build control."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .ledger import BuildControl
from .process import run_command
from .values import MAX_SECONDS, ControlError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("init")
    create.add_argument("--run-id", required=True)
    create.add_argument("--started-at", required=True)
    create.add_argument("--budget-usd", default="100")
    create.add_argument("--worker-cap", type=int, default=2)
    create.add_argument("--duration-seconds", type=int, default=MAX_SECONDS)
    create.add_argument("--admission-buffer-seconds", type=int, default=1800)
    commands.add_parser("status")
    reserve = commands.add_parser("reserve")
    reserve.add_argument("call_id")
    reserve.add_argument("--label", required=True)
    reserve.add_argument("--paid", action="store_true")
    reserve.add_argument("--max-cost-usd")
    reserve.add_argument("--without-worker", action="store_true")
    start = commands.add_parser("start")
    start.add_argument("call_id")
    start.add_argument("--worker-identity", required=True)
    finish = commands.add_parser("finish")
    finish.add_argument("call_id")
    finish.add_argument(
        "--outcome", choices=["succeeded", "failed", "interrupted", "unknown"], required=True
    )
    finish.add_argument("--process-stopped", action="store_true")
    finish.add_argument("--actual-cost-usd")
    finish.add_argument("--evidence-ref", required=True)
    finish.add_argument("--reason")
    settle = commands.add_parser("settle")
    settle.add_argument("call_id")
    settle.add_argument("--actual-cost-usd", required=True)
    settle.add_argument("--evidence-ref", required=True)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("destination", type=Path)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("call_id")
    cancel.add_argument("--confirm-not-started", action="store_true", required=True)
    cancel.add_argument("--evidence-ref", required=True)
    cap = commands.add_parser("worker-cap")
    cap.add_argument("value", type=int)
    cap.add_argument("--evidence-ref", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("call_id")
    execute.add_argument("--paid", action="store_true")
    execute.add_argument("--max-cost-usd")
    execute.add_argument("--timeout-seconds", type=float, required=True)
    execute.add_argument("--output-directory", type=Path, required=True)
    execute.add_argument(
        "--command-json",
        type=json.loads,
        required=True,
        help="Command argv as a JSON array; no shell is used.",
    )
    arguments = parser.parse_args(argv)
    control = BuildControl(arguments.ledger)
    try:
        match arguments.action:
            case "init":
                result = control.initialize(
                    run_id=arguments.run_id,
                    started_at=arguments.started_at,
                    budget_usd=arguments.budget_usd,
                    worker_cap=arguments.worker_cap,
                    duration_seconds=arguments.duration_seconds,
                    admission_buffer_seconds=arguments.admission_buffer_seconds,
                )
            case "status":
                result = control.snapshot()
            case "reserve":
                result = control.reserve(
                    arguments.call_id,
                    label=arguments.label,
                    paid=arguments.paid,
                    max_cost_usd=arguments.max_cost_usd,
                    uses_worker=not arguments.without_worker,
                )
            case "start":
                result = control.mark_started(
                    arguments.call_id, worker_identity=arguments.worker_identity
                )
            case "finish":
                result = control.record_outcome(
                    arguments.call_id,
                    outcome=arguments.outcome,
                    process_stopped=arguments.process_stopped,
                    actual_cost_usd=arguments.actual_cost_usd,
                    evidence_ref=arguments.evidence_ref,
                    reason=arguments.reason,
                )
            case "settle":
                result = control.settle_cost(
                    arguments.call_id,
                    actual_cost_usd=arguments.actual_cost_usd,
                    evidence_ref=arguments.evidence_ref,
                )
            case "cancel":
                result = control.cancel_unstarted(
                    arguments.call_id,
                    confirmed_not_started=arguments.confirm_not_started,
                    evidence_ref=arguments.evidence_ref,
                )
            case "worker-cap":
                result = control.set_worker_cap(
                    arguments.value, evidence_ref=arguments.evidence_ref
                )
            case "checkpoint":
                result = control.checkpoint(arguments.destination)
            case "run":
                result = run_command(
                    control,
                    call_id=arguments.call_id,
                    command=arguments.command_json,
                    paid=arguments.paid,
                    max_cost_usd=arguments.max_cost_usd,
                    timeout_seconds=arguments.timeout_seconds,
                    output_directory=arguments.output_directory,
                )
        print(json.dumps(result, indent=2))
        if arguments.action == "run":
            call = result["call"]
            confirmed_success = (
                call["status"] == "succeeded"
                and call["process_stopped"]
                and result["returncode"] == 0
            )
            return 0 if confirmed_success else 1
        return 0
    except (ControlError, sqlite3.Error, OSError) as error:
        code = error.code if isinstance(error, ControlError) else "storage_or_process_error"
        print(json.dumps({"error": {"code": code, "message": str(error)}}), file=sys.stderr)
        return 2
