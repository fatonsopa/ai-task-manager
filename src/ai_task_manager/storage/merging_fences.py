"""Indexed canonical publication state; history is retained separately as typed events."""

import json
from dataclasses import asdict

from ai_task_manager.config.files import strict_json
from ai_task_manager.domains.merging.contracts import oid, refname, sha, token
from ai_task_manager.domains.work.errors import EvidenceRequired, SchemaError, ValidationError

INTENT_KEYS = {
    "task_id",
    "attempt_id",
    "issue_id",
    "prose_digest",
    "task_version",
    "attempt_version",
    "decision_revision",
    "decision_ref",
    "operation_ref",
    "binding_digest",
    "commit",
    "old",
    "source_ref",
    "source_head",
    "target_ref",
    "candidate_ref",
    "checks_ref",
}


def decode(raw):
    try:
        value = strict_json(raw)
        required = {"issue_key", "context", "intent", "state"}
        if not required <= value.keys() or set(value) - required - {
            "actual_merge_ref",
            "task_version",
            "decision_revision",
        }:
            raise ValueError
        value = {
            key: item
            for key, item in value.items()
            if key not in {"task_version", "decision_revision"}
        }
        sha(value["issue_key"])
        context, intent = value["context"], value["intent"]
        if set(context) != {"reference", "binding_digest", "commit"} or set(intent) != INTENT_KEYS:
            raise ValueError
        token(context["reference"])
        sha(context["binding_digest"])
        oid(context["commit"])
        for key in ("prose_digest", "binding_digest"):
            sha(intent[key])
        for key in ("commit", "old", "source_head"):
            oid(intent[key])
        for key in ("source_ref", "target_ref", "candidate_ref"):
            refname(intent[key])
        for key in (
            "task_id",
            "attempt_id",
            "issue_id",
            "decision_ref",
            "operation_ref",
            "checks_ref",
        ):
            token(intent[key])
        for key in ("task_version", "attempt_version", "decision_revision"):
            if type(intent[key]) is not int or intent[key] < 0:
                raise ValueError
        if (context["binding_digest"], context["commit"]) != (
            intent["binding_digest"],
            intent["commit"],
        ):
            raise ValueError
        if value["state"] == "committed":
            token(value["actual_merge_ref"])
        elif value["state"] != "pending" or "actual_merge_ref" in value:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
        raise SchemaError("Canonical publication state is malformed") from exc


def current(conn, issue_key):
    row = conn.execute(
        "SELECT context_ref,state,data FROM publication_fences WHERE issue_key=?", (issue_key,)
    ).fetchone()
    if row is None:
        return None
    value = decode(row[2])
    if (value["issue_key"], value["context"]["reference"], value["state"]) != (
        issue_key,
        row[0],
        row[1],
    ):
        raise SchemaError("Publication projection index and record differ")
    return value


def insert(conn, value):
    value = decode(json.dumps(value))
    conn.execute(
        "INSERT INTO publication_fences VALUES(?,?,?,?)",
        (
            value["issue_key"],
            value["context"]["reference"],
            value["state"],
            json.dumps(value, sort_keys=True),
        ),
    )


def commit(conn, value):
    value = decode(json.dumps(value))
    changed = conn.execute(
        "UPDATE publication_fences SET state=?,data=? WHERE issue_key=? "
        "AND context_ref=? AND state='pending'",
        (
            value["state"],
            json.dumps(value, sort_keys=True),
            value["issue_key"],
            value["context"]["reference"],
        ),
    ).rowcount
    if changed != 1:
        raise SchemaError("Publication projection ownership changed")


def backfill(conn, value):
    old = current(conn, value["issue_key"])
    if old is None:
        insert(conn, value)
    elif (
        old["context"] != value["context"]
        or old["intent"] != value["intent"]
        or (old["state"] == "committed" and old != value)
    ):
        raise SchemaError("Publication history has conflicting or regressing authority")
    elif old["state"] == "pending" and value["state"] == "committed":
        commit(conn, value)


def pending(conn, issue_key):
    value = current(conn, issue_key)
    return [value] if value and value["state"] == "pending" else []


def require_clear(conn, issue_key, context=None):
    value = current(conn, issue_key)
    if value and (
        value["state"] == "committed" or context is None or value["context"] != asdict(context)
    ):
        raise EvidenceRequired("Pending, uncertain or committed publication prevents new work")
    if context is not None and value is None:
        raise EvidenceRequired("The exact pending publication authority is unavailable")
