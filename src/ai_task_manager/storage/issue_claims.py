"""Canonical lifecycle facts outlive mutable provider status and display names."""

import hashlib
import json

from ai_task_manager.domains.work.errors import EvidenceRequired, SchemaError
from ai_task_manager.domains.work.models import DELIVERED, IssueStatus, Scope


def identity(scope, issue_id):
    raw = json.dumps(
        {"provider": scope.provider, "workspace": scope.workspace, "issue_id": issue_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def fence(conn, scope, issue_id, *, delivered=False, terminal=False, source=""):
    key = identity(scope, issue_id)
    conn.execute(
        "INSERT INTO issue_lifecycle VALUES(?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET "
        "delivered=MAX(delivered,excluded.delivered),terminal=MAX(terminal,excluded.terminal)",
        (key, int(delivered), int(terminal), source),
    )


def _facts(conn, scope, issue_id):
    row = conn.execute(
        "SELECT delivered,terminal FROM issue_lifecycle WHERE identity_key=?",
        (identity(scope, issue_id),),
    ).fetchone()
    if row and any(type(value) is not int or value not in {0, 1} for value in row):
        raise SchemaError("Canonical lifecycle facts are invalid")
    return row


def known_delivered(conn, scope, task):
    """Positive authority only; absence never proves a safe provider history."""
    row = _facts(conn, scope, task.issue_id)
    return task.delivered_once or bool(row and row[0])


def require_open(conn, scope, issue_id):
    row = _facts(conn, scope, issue_id)
    if row and (row[0] or row[1]):
        raise EvidenceRequired("Delivered or terminal history requires a new authorized issue")


def imported(conn, task_id):
    return conn.execute("SELECT * FROM existing_admissions WHERE task_id=?", (task_id,)).fetchone()


def seed_lifecycle(conn):
    """Carry positive old facts; missing old history never becomes negative proof."""
    for row in conn.execute(
        "SELECT tasks.id,tasks.issue_id,tasks.data,sessions.scope FROM tasks JOIN sessions "
        "ON sessions.id=tasks.session_id WHERE tasks.issue_id IS NOT NULL"
    ).fetchall():
        task, scope = json.loads(row["data"]), Scope(**json.loads(row["scope"]))
        statuses = [task.get("observed_status")]
        for event in conn.execute(
            "SELECT payload FROM events WHERE task_id=? AND kind='provider_status_confirmed'",
            (row["id"],),
        ).fetchall():
            statuses.append(json.loads(event[0]).get("observed_status"))
        for observation in conn.execute(
            "SELECT payload FROM external_observations WHERE task_id=?", (row["id"],)
        ).fetchall():
            data = json.loads(observation[0])
            if data.get("issue_id") == row["issue_id"]:
                statuses.append(data.get("status"))
        delivered = task.get("delivered_once") is True or any(
            value in DELIVERED for value in statuses
        )
        terminal = any(value in {IssueStatus.CANCELED, IssueStatus.DUPLICATE} for value in statuses)
        if delivered or terminal:
            fence(
                conn,
                scope,
                row["issue_id"],
                delivered=delivered,
                terminal=terminal,
                source="schema-2-positive-history",
            )
