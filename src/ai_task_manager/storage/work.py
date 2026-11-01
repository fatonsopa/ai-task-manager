import hashlib
import json
from dataclasses import replace

from ai_task_manager.domains.work.errors import (
    IdempotencyConflict,
    NotFound,
    StaleVersion,
)
from ai_task_manager.domains.work.models import IssueStatus, SyncState, Task

from .database import encode, record


def task_from(data: dict) -> Task:
    data = dict(data)
    for key in ("required_work", "completed_work"):
        data[key] = tuple(data[key])
    if data["observed_status"] is not None:
        data["observed_status"] = IssueStatus(data["observed_status"])
    data["desired_status"] = IssueStatus(data["desired_status"])
    data["sync_state"] = SyncState(data["sync_state"])
    return Task(**data)


def get(conn, session_id: str, task_id: str) -> Task:
    row = conn.execute(
        "SELECT data FROM tasks WHERE id=? AND session_id=?", (task_id, session_id)
    ).fetchone()
    if row is None:
        raise NotFound("Task is not available in this session")
    return task_from(json.loads(row[0]))


def insert(conn, task: Task):
    conn.execute(
        "INSERT INTO tasks VALUES(?,?,?,?,?)",
        (task.id, task.session_id, task.issue_id, task.version, record(task)),
    )


def save(conn, task: Task, expected_version: int) -> Task:
    task = replace(task, version=expected_version + 1)
    result = conn.execute(
        "UPDATE tasks SET issue_id=?,version=?,data=? WHERE id=? AND version=?",
        (task.issue_id, task.version, record(task), task.id, expected_version),
    )
    if result.rowcount != 1:
        raise StaleVersion("Task version has changed")
    return task


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(encode(payload).encode()).hexdigest()


def replay(conn, session_id: str, key: str, payload: dict):
    if not isinstance(key, str) or not key.strip():
        raise IdempotencyConflict("An operation identity is required")
    row = conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
    if row:
        if row["session_id"] != session_id or row["fingerprint"] != fingerprint(payload):
            raise IdempotencyConflict("Operation identity already has a different request")
        return json.loads(row["result"])
    return None


def remember(conn, session_id: str, key: str, payload: dict, result: dict):
    conn.execute(
        "INSERT INTO operations VALUES(?,?,?,?)",
        (key, session_id, fingerprint(payload), encode(result)),
    )
