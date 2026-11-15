"""Explicit input schemas and public error categories."""

import json
from dataclasses import asdict, is_dataclass

from jsonschema import Draft202012Validator

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work import errors
from ai_task_manager.domains.work.existing import ExistingBlocked
from ai_task_manager.domains.work.models import Cause, IssueStatus
from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeUncertain


class Unavailable(errors.WorkError):
    pass


def obj(properties=None, required=()):
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}
REF = {"type": "string", "minLength": 1, "maxLength": 256}
VERSION = {"type": "integer", "minimum": 0}
TASK = {"task_id": REF}
WRITE = {"writing_ref": REF, "operation_key": REF}
CONCURRENCY = {"expected_version": VERSION, "decision_revision": VERSION, "operation_key": REF}
SCHEMAS = {
    "workflow_run": obj(
        {"task_id": REF, "plan_ref": REF, "operation_key": REF},
        ("task_id", "plan_ref", "operation_key"),
    ),
    "workflow_resume": obj({"workflow_ref": REF}, ("workflow_ref",)),
    "session_show": obj(),
    "session_resume": obj(
        {"enrollment_ref": REF, "resume_ref": REF}, ("enrollment_ref", "resume_ref")
    ),
    "writing_prepare": obj(
        {
            "kind": {
                "enum": [
                    "issue_" + x
                    for x in ("bug", "chore", "decision", "feature", "improvement", "release")
                ]
            },
            "purpose": {"enum": ["task", "progress"]},
            "title": {"type": "string", "maxLength": 300},
            "body": {"type": "string", "minLength": 1, "maxLength": 100000},
            "code_bearing": {"type": "boolean"},
            "required_work": {"type": "array", "items": TEXT, "minItems": 1, "uniqueItems": True},
            "task_id": REF,
        },
        ("kind", "purpose", "title", "body", "code_bearing", "required_work"),
    ),
    "writing_review": obj(
        {"writing_id": REF, "review": {"type": "object"}}, ("writing_id", "review")
    ),
    "writing_finalize": obj({"writing_id": REF}, ("writing_id",)),
    "task_submit": obj({**WRITE, "parent_task_id": REF, "followup_ref": REF}, tuple(WRITE)),
    "task_find": obj(
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "cursor": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
        ("query",),
    ),
    "task_inspect": obj(
        {"issue": {"type": "string", "minLength": 1, "maxLength": 128}}, ("issue",)
    ),
    "task_admit": obj(
        {"discovery_ref": REF, "writing_ref": REF, "selection_ref": REF, "operation_key": REF},
        ("discovery_ref", "writing_ref", "selection_ref", "operation_key"),
    ),
    "task_select": obj(
        {"discovery_ref": REF, "operation_key": REF}, ("discovery_ref", "operation_key")
    ),
    "task_list": obj(),
    "task_show": obj(TASK, ("task_id",)),
    "task_external_changes": obj(TASK, ("task_id",)),
    "task_accept_external_prose": obj(
        {**TASK, **WRITE, **CONCURRENCY, "observation_id": REF},
        ("task_id", "writing_ref", "observation_id", *CONCURRENCY),
    ),
    "task_settle_external": obj(
        {
            **TASK,
            **CONCURRENCY,
            "observation_id": REF,
            "cause": {"enum": list(Cause)},
            "evidence_refs": {"type": "object", "additionalProperties": REF},
        },
        ("task_id", "observation_id", "cause", *CONCURRENCY),
    ),
    "task_action": obj(
        {
            **TASK,
            **CONCURRENCY,
            "action": {
                "enum": [
                    "enqueue",
                    "dequeue",
                    "hold",
                    "request_review",
                    "review_decision",
                    "cancel",
                    "duplicate",
                    "merge",
                    "stage",
                    "release",
                    "non_code_complete",
                ]
            },
            "human_ref": REF,
            "evidence_refs": {"type": "object", "additionalProperties": REF},
            "review_target": {"enum": list(IssueStatus)},
            "reason_writing_ref": REF,
        },
        ("task_id", "action", *CONCURRENCY),
    ),
    "task_revise": obj(
        {**TASK, **WRITE, **CONCURRENCY, "human_ref": REF},
        ("task_id", "writing_ref", "human_ref", *CONCURRENCY),
    ),
    "task_decision_prepare": obj(
        {
            **TASK,
            "operation_key": REF,
            "title": TEXT,
            "context": TEXT,
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 8,
                "items": obj(
                    {"id": REF, "label": TEXT, "description": TEXT}, ("id", "label", "description")
                ),
            },
            "recommendation": REF,
            "rationale": TEXT,
        },
        ("task_id", "operation_key", "title", "context", "options", "recommendation", "rationale"),
    ),
    "task_decision_request": obj({**TASK, **WRITE}, ("task_id", *WRITE)),
    "task_decision_resolve": obj(
        {"request_id": REF, "human_ref": REF, "operation_key": REF},
        ("request_id", "human_ref", "operation_key"),
    ),
    "task_decision_show": obj({"request_id": REF}, ("request_id",)),
    "task_progress": obj({**TASK, **WRITE}, ("task_id", *WRITE)),
    "worker_start": obj(
        {**TASK, **CONCURRENCY, "host": REF, "human_ref": REF}, ("task_id", "host", *CONCURRENCY)
    ),
    "worker_execute": obj(
        {"attempt_id": REF, "command_ref": REF, "operation_key": REF},
        ("attempt_id", "command_ref", "operation_key"),
    ),
    "event_list": obj(
        {"after": VERSION, "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 60}}
    ),
    "event_acknowledge": obj(
        {
            "event_id": REF,
            "stage": {"enum": ["delivered", "ai_acknowledged", "user_reported"]},
            "observation_ref": REF,
            "operation_key": REF,
        },
        ("event_id", "stage", "observation_ref", "operation_key"),
    ),
    "sync_now": obj(),
}


def validate(operation, arguments):
    if operation not in SCHEMAS:
        raise errors.ValidationError("Unknown application operation")
    if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()) > 262144:
        raise errors.ValidationError("Request exceeds the configured size limit")
    if next(Draft202012Validator(SCHEMAS[operation]).iter_errors(arguments), None):
        raise errors.ValidationError("Arguments do not match the operation schema")


def public(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {
            k: public(v)
            for k, v in value.items()
            if k not in {"token", "capability", "secret", "api_key"}
        }
    if isinstance(value, (tuple, list)):
        return [public(v) for v in value]
    return value


def result(data, outcome="confirmed"):
    return {"ok": True, "outcome": outcome, "data": public(data)}


def external_result(data):
    state = data.get("state") if isinstance(data, dict) else None
    if state in {"pending", "sending", "leased"}:
        return result(data, "pending")
    if state == "uncertain":
        return result(data, "uncertain")
    if state in {"failed", "conflict"}:
        return {
            "ok": False,
            "outcome": "failed",
            "data": public(data),
            "error": {
                "category": "failed",
                "message": "External operation did not settle successfully",
            },
            "exit_code": 5,
        }
    return result(data)


def failure(error):
    if isinstance(error, LinearError):
        if error.uncertain:
            category, code = "uncertain", 6
        elif error.category in {"scope_mismatch", "authentication", "credential_binding_changed"}:
            category, code = "denied", 3
        elif error.category in {"rate_limited", "synchronization_pending"}:
            category, code = "blocked", 4
        else:
            category, code = "failed", 5
    elif isinstance(error, (RuntimeRefused, RuntimeUncertain)):
        category, code = ("blocked", 4) if isinstance(error, RuntimeRefused) else ("uncertain", 6)
    elif isinstance(
        error,
        (
            errors.ValidationError,
            errors.StaleVersion,
            errors.IdempotencyConflict,
            errors.SchemaError,
        ),
    ):
        category, code = "invalid_request", 2
    elif isinstance(
        error,
        (
            errors.AccessDenied,
            errors.ApprovalRequired,
            errors.EvidenceRequired,
            errors.InvalidTransition,
        ),
    ):
        category, code = "denied", 3
    elif isinstance(
        error, (Unavailable, ExistingBlocked, SchedulingBlocked, errors.CapacityExceeded)
    ):
        category, code = "blocked", 4
    else:
        category, code = "failed", 5
    # Unclassified adapter diagnostics may contain private request material.
    message = (
        str(error) if code < 5 else "Operation failed or is uncertain; completion is not confirmed"
    )
    return {
        "ok": False,
        "outcome": category,
        "error": {"category": category, "message": message},
        "exit_code": code,
    }


def exit_code(envelope):
    return envelope.get(
        "exit_code", 6 if envelope.get("outcome") in {"pending", "uncertain"} else 0
    )
