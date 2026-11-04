"""Bounded causal metadata without messages, locals, paths or source-file I/O."""

import hashlib
import marshal
import math
import sqlite3
import sys
import time
from enum import StrEnum
from types import ModuleType

from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work import errors


class FailureStage(StrEnum):
    AUTHORITY_REFRESH = "authority_refresh"
    IDENTITY_REFRESH = "identity_refresh"
    AUTHORITY_MONITOR = "authority_monitor"
    AUTHORITY_STOP = "authority_stop"
    AUTHORITY_CALLBACK = "authority_callback"
    STARTUP_MONITOR = "startup_monitor"
    NATIVE_TURN = "native_turn"
    NATIVE_TRANSPORT = "native_transport"
    NATIVE_STOP = "native_stop"
    NATIVE_DISCONNECT = "native_disconnect"
    MANAGED_START = "managed_start"
    MANAGED_EXECUTION = "managed_execution"
    MANAGED_SETTLEMENT = "managed_settlement"
    MANAGED_STOP = "managed_stop"
    RECEIVER_LOAD = "receiver_load"
    RECEIVER_CONFIGURE = "receiver_configure"
    RECEIVER_START = "receiver_start"
    RECEIVER_MONITOR = "receiver_monitor"
    RECEIVER_STOP = "receiver_stop"
    DIAGNOSTIC_RETENTION = "diagnostic_retention"


_TYPES = {
    sqlite3.OperationalError: "sqlite_operational_error",
    errors.WorkError: "work_error",
    errors.ValidationError: "validation_error",
    errors.AccessDenied: "access_denied",
    errors.NotFound: "not_found",
    errors.InvalidTransition: "invalid_transition",
    errors.StaleVersion: "stale_version",
    errors.StaleDecision: "stale_decision",
    errors.CapacityExceeded: "capacity_exceeded",
    errors.IdempotencyConflict: "idempotency_conflict",
    errors.ApprovalRequired: "approval_required",
    errors.EvidenceRequired: "evidence_required",
    errors.SchemaError: "schema_error",
    SchedulingBlocked: "scheduling_blocked",
    AssertionError: "assertion_error",
    RuntimeError: "runtime_error",
    TypeError: "type_error",
    ValueError: "value_error",
    TimeoutError: "timeout",
    OSError: "os_error",
    PermissionError: "permission_error",
    FileNotFoundError: "file_not_found",
    ConnectionError: "connection_error",
    BrokenPipeError: "broken_pipe",
    KeyboardInterrupt: "interrupted",
    Exception: "exception",
    BaseException: "base_exception",
}
MAX_CAUSES, MAX_FRAMES, MAX_TRACEBACK = 4, 8, 64


def _number(value):
    return value if type(value) in {float, int} and math.isfinite(value) else None


def _frames(error):
    current = BaseException.__traceback__.__get__(error)
    kept, omitted, walked = [], 0, 0
    while current is not None and walked < MAX_TRACEBACK:
        walked += 1
        frame, line = current.tb_frame, current.tb_lineno
        namespace = frame.f_globals
        name = namespace.get("__name__")
        module = sys.modules.get(name) if type(name) is str else None
        if (
            type(name) is str
            and name.startswith("ai_task_manager.")
            and type(module) is ModuleType
            and object.__getattribute__(module, "__dict__") is namespace
        ):
            code = frame.f_code
            # Loaded compiled-code identity is diagnostic metadata, not a file
            # seal, authentication observation or authority proof.
            code_hash = hashlib.sha256(marshal.dumps(code)).hexdigest()
            kept.append(
                {"module": name, "function": code.co_name, "line": line, "code_sha256": code_hash}
            )
            if len(kept) > MAX_FRAMES:
                kept.pop(0)  # Preserve the innermost owned origin.
                omitted += 1
        else:
            omitted += 1
        current = current.tb_next
    return kept, omitted, current is not None


def capture_failure(error, stage, *, started=None):
    """Use builtin exception descriptors, never arbitrary stringifiers/getters.

    Stage and type vocabularies are closed. Foreign exception classes/frames,
    traceback locals, args and exception messages are intentionally omitted.
    This function is best-effort observation and must never replace a failure.
    """
    report = {
        "version": 1,
        "stage": stage.value if type(stage) is FailureStage else "unknown",
        "observed_at": None,
        "elapsed_seconds": None,
        "causes": [],
        "chain_truncated": False,
        "projection_failed": False,
    }
    try:
        report["observed_at"] = _number(time.time())
        start = _number(started)
        if start is not None:
            report["elapsed_seconds"] = _number(max(0.0, time.monotonic() - start))
        current, seen = error, set()
        while isinstance(current, BaseException) and len(report["causes"]) < MAX_CAUSES:
            if id(current) in seen:
                report["chain_truncated"] = True
                break
            seen.add(id(current))
            frames, omitted, truncated = _frames(current)
            error_type = type(current)
            code = next(
                (value for known, value in _TYPES.items() if error_type is known), "unclassified"
            )
            report["causes"].append(
                {
                    "code": code,
                    "type": error_type.__name__ if code != "unclassified" else None,
                    "frames": frames,
                    "foreign_frames_omitted": omitted,
                    "traceback_truncated": truncated,
                }
            )
            cause = BaseException.__cause__.__get__(current)
            if cause is not None:
                current = cause
            elif not BaseException.__suppress_context__.__get__(current):
                current = BaseException.__context__.__get__(current)
            else:
                current = None
        if current is not None and len(report["causes"]) >= MAX_CAUSES:
            report["chain_truncated"] = True
    except BaseException:
        report["projection_failed"] = True
    return report
