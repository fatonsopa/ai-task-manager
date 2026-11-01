"""One bounded external process, with explicit launch and outcome records."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .ledger import BuildControl
from .values import ControlError, _require, _utc


def run_command(
    control: BuildControl,
    *,
    call_id: str,
    command: list[str],
    paid: bool,
    max_cost_usd: str | None,
    timeout_seconds: float,
    output_directory: str | Path,
) -> dict[str, Any]:
    """Run one bounded argv without a shell; charges stay reserved until reconciled."""
    _require(
        isinstance(command, list)
        and bool(command)
        and all(isinstance(arg, str) and arg for arg in command),
        "invalid_input",
        "A command argument list is required.",
    )
    _require(
        isinstance(timeout_seconds, (int, float))
        and (not isinstance(timeout_seconds, bool))
        and (0 < timeout_seconds < float("inf")),
        "invalid_input",
        "Timeout must be positive and finite.",
    )
    reserved = control.reserve(
        call_id, label="bounded command", paid=paid, max_cost_usd=max_cost_usd
    )
    _require(
        reserved["status"] == "reserved",
        "already_executed",
        "This invocation has already started or finished; reconcile instead of relaunching.",
    )
    invocation = str(uuid.uuid4())
    control.claim_launch(call_id, worker_identity=invocation)
    output = Path(output_directory).absolute()
    stdout_path, stderr_path = (output / f"{call_id}.stdout", output / f"{call_id}.stderr")
    process: subprocess.Popen[bytes] | None = None
    try:
        output.mkdir(parents=True, exist_ok=True, mode=448)
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(stdout_path, 384)
            os.chmod(stderr_path, 384)
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, start_new_session=True
            )
            identity = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(process.pid)],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            control.mark_started(
                call_id,
                worker_identity=invocation,
                process_pid=process.pid if identity else None,
                process_start_identity=f"ps:{identity}" if identity else None,
            )
            remaining = (
                _utc(control.snapshot()["deadline"]) - _utc(control.clock())
            ).total_seconds()
            try:
                code = process.wait(timeout=max(0.001, min(timeout_seconds, remaining)))
                outcome, reason = ("succeeded", None) if code == 0 else ("failed", f"exit:{code}")
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                code, outcome, reason = (process.returncode, "interrupted", "timeout")
            result = control.record_outcome(
                call_id,
                outcome=outcome,
                process_stopped=True,
                evidence_ref=str(stdout_path),
                reason=reason,
            )
            return {
                "call": result,
                "returncode": code,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            }
    except FileExistsError as error:
        raise ControlError(
            "launch_uncertain", "Existing invocation traces require reconciliation."
        ) from error
    except BaseException as error:
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        try:
            control.record_outcome(
                call_id,
                outcome="failed",
                process_stopped=True,
                evidence_ref=str(stderr_path),
                reason=type(error).__name__,
                actual_cost_usd="0" if process is None else None,
            )
        except (ControlError, sqlite3.Error):
            pass
        raise
