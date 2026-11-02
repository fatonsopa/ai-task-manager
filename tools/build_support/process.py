"""One bounded external invocation, including its owned process group's lifetime."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .checkpoint import publish_checkpoint
from .ledger import BuildControl
from .process_group import OwnedProcessGroup, require_group_monitoring, supervise
from .values import ControlError, _require, _utc


def _evidence(
    control: BuildControl,
    destination: Path,
    *,
    stdout: Path,
    stderr: Path,
    completion: dict[str, Any],
) -> None:
    publish_checkpoint(
        {"stdout": str(stdout), "stderr": str(stderr), **completion}, destination, control.ledger
    )


def run_command(
    control: BuildControl,
    *,
    call_id: str,
    command: list[str],
    paid: bool,
    max_cost_usd: str | None,
    timeout_seconds: float,
    output_directory: str | Path,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Run one argv; keep reservations until the whole owned group is confirmed stopped."""
    _require(
        isinstance(command, list)
        and bool(command)
        and all(isinstance(arg, str) and arg for arg in command),
        "invalid_input",
        "A command argument list is required.",
    )
    _require(
        isinstance(timeout_seconds, (int, float))
        and not isinstance(timeout_seconds, bool)
        and 0 < timeout_seconds < float("inf"),
        "invalid_input",
        "Timeout must be positive and finite.",
    )
    try:
        require_group_monitoring()
    except RuntimeError as error:
        raise ControlError("process_monitor_unavailable", str(error)) from error
    reserved = control.reserve(
        call_id,
        label="bounded command",
        paid=paid,
        max_cost_usd=max_cost_usd,
        batch_id=batch_id,
        timeout_seconds=timeout_seconds,
    )
    _require(
        reserved["status"] == "reserved",
        "already_executed",
        "This invocation has already started or finished; reconcile instead of relaunching.",
    )
    invocation = str(uuid.uuid4())
    control.claim_launch(call_id, worker_identity=invocation)
    output = Path(output_directory).absolute()
    stdout_path = output / f"{call_id}.stdout"
    stderr_path = output / f"{call_id}.stderr"
    group_path = output / f"{call_id}.process-group.json"
    process: subprocess.Popen[bytes] | None = None
    group: OwnedProcessGroup | None = None
    completion: dict[str, Any] | None = None
    try:
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            with group_path.open("x") as pending:
                os.chmod(group_path, 0o600)
                json.dump({"status": "launch_claimed", "call_id": call_id}, pending)
                pending.flush()
                os.fsync(pending.fileno())
            boundary = (
                control.launch_boundary(call_id, worker_identity=invocation)
                if reserved.get("invocation_bound")
                else nullcontext(None)
            )
            with boundary as record_started:
                process = subprocess.Popen(
                    command, stdout=stdout, stderr=stderr, start_new_session=True
                )
                group = OwnedProcessGroup(process)
                group.capture()
                identity = group.start_identity
                observed = {
                    "process_pid": process.pid if identity else None,
                    "process_start_identity": f"ps:{identity}" if identity else None,
                }
                if record_started is not None:
                    record_started(**observed)
                else:
                    control.mark_started(call_id, worker_identity=invocation, **observed)
            snapshot = control.snapshot()
            current_call = next(c for c in snapshot["calls"] if c["call_id"] == call_id)
            bound = current_call.get("invocation_bound")
            deadline = bound["execution_deadline"] if bound else snapshot["deadline"]
            remaining = (_utc(deadline) - _utc(control.clock())).total_seconds()
            completion = supervise(
                group, timeout_seconds=max(0.001, min(timeout_seconds, remaining))
            )
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
            _evidence(
                control, group_path, stdout=stdout_path, stderr=stderr_path, completion=completion
            )
            result = control.record_outcome(
                call_id,
                outcome=completion["outcome"],
                process_stopped=completion["group"]["confirmed_stop"],
                evidence_ref=str(group_path),
                reason=completion["reason"],
            )
            return {
                "call": result,
                "returncode": completion["returncode"],
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "group": completion["group"],
                "group_evidence": str(group_path),
            }
    except BaseException as error:
        if isinstance(error, FileExistsError) and process is None:
            # Prior traces can represent another or only partially recorded invocation.
            raise ControlError(
                "launch_uncertain", "Existing invocation traces require reconciliation."
            ) from error
        if completion is None:
            if group is not None:
                stop = group.stop()
                completion = {
                    "outcome": "failed" if stop.confirmed else "unknown",
                    "reason": type(error).__name__,
                    "returncode": group.reap_exited_leader(),
                    "group": group.evidence(stop, orphaned=False),
                }
            else:
                completion = {
                    "outcome": "failed",
                    "reason": type(error).__name__,
                    "returncode": None,
                    "group": {
                        "group_id": None,
                        "leader_pid": None,
                        "orphaned": False,
                        "confirmed_stop": process is None,
                        "signals": [],
                        "observation": {
                            "known": process is None,
                            "reason": "process_not_created",
                            "members": [],
                            "live_pids": [] if process is None else None,
                        },
                    },
                }
        try:
            _evidence(
                control, group_path, stdout=stdout_path, stderr=stderr_path, completion=completion
            )
        except OSError:
            pass  # Keep the durable reservation if evidence storage itself failed.
        try:
            control.record_outcome(
                call_id,
                outcome="failed" if completion["group"]["confirmed_stop"] else "unknown",
                process_stopped=completion["group"]["confirmed_stop"],
                evidence_ref=str(group_path),
                reason=type(error).__name__,
                actual_cost_usd="0" if process is None else None,
            )
        except (ControlError, sqlite3.Error):
            pass
        raise
