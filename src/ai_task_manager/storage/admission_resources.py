"""Conflict claims share Core's existing resource leases and transaction."""

import json

from ai_task_manager.domains.scheduling.resources import file_overlap
from ai_task_manager.domains.work.errors import CapacityExceeded, EvidenceRequired

from .database import encode


def owner(scope):
    return [scope.provider, scope.workspace, scope.project]


def declared(conn, task_id):
    row = conn.execute("SELECT payload FROM events WHERE task_id=? "
                       "AND kind='scheduling_resources_declared' ORDER BY sequence DESC LIMIT 1",
                       (task_id,)).fetchone()
    return json.loads(row[0])["declaration"] if row else None


def claims(conn, scope, declaration, *, task_id=None):
    if conn.execute(
        "SELECT 1 FROM attempts a WHERE a.active=1 AND a.task_id!=? "
        "AND NOT EXISTS(SELECT 1 FROM resource_leases l "
        "WHERE l.attempt_id=a.id AND l.kind='admission') LIMIT 1", (task_id or "",),
    ).fetchone():
        raise CapacityExceeded("Existing task resource ownership is unresolved")
    for row in conn.execute(
        "SELECT l.resource_id,a.task_id FROM resource_leases l "
        "JOIN attempts a ON a.id=l.attempt_id WHERE l.kind='admission'"
    ):
        try:
            previous = json.loads(row[0])
            files, locks = previous["files"], {tuple(p) for p in previous["locks"]}
            repository = previous["repository"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EvidenceRequired("Resource ownership cannot be reconciled") from exc
        if row[1] == task_id:
            continue
        common = set(declaration.locks).intersection(locks)
        if any(pair[0] == "resource" for pair in common):
            raise CapacityExceeded("Declared task resources are occupied")
        if repository != declaration.repository:
            continue
        if (any(file_overlap(left, right) for left in declaration.files
                for right in files) or common):
            raise CapacityExceeded("Declared task resources are occupied")


def reserve(conn, scope, declaration, attempt):
    claims(conn, scope, declaration, task_id=attempt.task_id)
    value = encode({
        "scope": owner(scope), "repository": declaration.repository,
        "files": declaration.files, "locks": declaration.locks,
        "task_id": attempt.task_id, "attempt_id": attempt.id,
        "prose_digest": declaration.prose_digest,
        "decision_revision": declaration.decision_revision,
    })
    conn.execute("INSERT INTO resource_leases VALUES(?,?,?)", ("admission", value, attempt.id))


def require_held(conn, scope, declaration, attempt):
    row = conn.execute("SELECT resource_id FROM resource_leases "
                       "WHERE kind='admission' AND attempt_id=?", (attempt.id,)).fetchall()
    if len(row) != 1:
        raise EvidenceRequired("Original atomic resource reservation is unavailable")
    expected = {
        "scope": owner(scope), "repository": declaration.repository,
        "files": list(declaration.files), "locks": [list(p) for p in declaration.locks],
        "task_id": attempt.task_id, "attempt_id": attempt.id,
        "prose_digest": declaration.prose_digest,
        "decision_revision": declaration.decision_revision,
    }
    if json.loads(row[0][0]) != expected:
        raise EvidenceRequired("Current resource declaration differs from its reservation")
