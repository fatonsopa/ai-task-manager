import json

from ai_task_manager.domains.work.errors import NotFound
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS, Attempt, AttemptState

from .database import record


def attempt_from(data: dict) -> Attempt:
    return Attempt(**{**data, "state": AttemptState(data["state"])})


def get(conn, session_id: str, attempt_id: str) -> Attempt:
    row = conn.execute(
        "SELECT data FROM attempts WHERE id=? AND session_id=?",
        (attempt_id, session_id),
    ).fetchone()
    if row is None:
        raise NotFound("Attempt is not available in this session")
    from .sessions import require_task

    result = attempt_from(json.loads(row[0]))
    require_task(session_id, result.task_id)
    return result


def active(conn, task_id: str) -> Attempt | None:
    row = conn.execute(
        "SELECT data FROM attempts WHERE task_id=? AND active=1", (task_id,)
    ).fetchone()
    return attempt_from(json.loads(row[0])) if row else None


def insert(conn, attempt: Attempt):
    conn.execute(
        "INSERT INTO attempts VALUES(?,?,?,?,?,?)",
        (
            attempt.id,
            attempt.task_id,
            attempt.session_id,
            attempt.host,
            int(attempt.state in ACTIVE_ATTEMPTS),
            record(attempt),
        ),
    )


def save(conn, attempt: Attempt):
    conn.execute(
        "UPDATE attempts SET active=?,data=? WHERE id=?",
        (int(attempt.state in ACTIVE_ATTEMPTS), record(attempt), attempt.id),
    )
