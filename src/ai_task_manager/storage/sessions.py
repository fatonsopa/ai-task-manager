import hashlib
import json
import secrets
from uuid import uuid4

from ai_task_manager.domains.work.errors import (
    AccessDenied,
    EvidenceRequired,
    ValidationError,
)
from ai_task_manager.domains.work.models import Scope, SessionHandle

from .database import record


class TaskSession(str):
    """A fresh capability retains history access only for its named tasks."""

    def __new__(cls, session_id, reference, task_ids):
        value = super().__new__(cls, session_id)
        value.reference, value.task_ids = reference, frozenset(task_ids)
        return value

    def __deepcopy__(self, memo):
        # Stored task/attempt identities retain the original scalar session ID.
        return str(self)


def permits(session_id, task_id):
    return not isinstance(session_id, TaskSession) or task_id in session_id.task_ids


def require_task(session_id, task_id):
    if not permits(session_id, task_id):
        raise AccessDenied("Successor capability does not authorize this task")


def require_request(conn, session_id, payload):
    if not isinstance(session_id, TaskSession):
        return
    task_id = payload.get("task_id")
    for field, table in (("attempt_id", "attempts"), ("operation_id", "outbox"),
                         ("event_id", "events")):
        if task_id is None and payload.get(field):
            row = conn.execute(
                f"SELECT task_id FROM {table} WHERE id=? AND session_id=?",
                (payload[field], session_id),
            ).fetchone()
            task_id = row[0] if row else None
    require_task(session_id, task_id)


def permits_delivery(conn, session_id, payload):
    if not isinstance(session_id, TaskSession):
        return True
    data = payload.get("data") if isinstance(payload, dict) else None
    issue_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(issue_id, str) or not issue_id:
        return False
    rows = conn.execute("SELECT id FROM tasks WHERE session_id=? AND issue_id=?",
                        (session_id, issue_id)).fetchall()
    return len(rows) == 1 and permits(session_id, rows[0][0])


def require_delivery(conn, session_id, payload):
    if not permits_delivery(conn, session_id, payload):
        raise AccessDenied("Successor capability does not authorize this inbound delivery")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def resolve(conn, handle: SessionHandle) -> tuple[str, Scope]:
    if not isinstance(handle, SessionHandle):
        raise AccessDenied("An opaque session capability is required")
    row = conn.execute(
        "SELECT id,scope FROM sessions WHERE token_hash=? AND revoked=0",
        (token_hash(handle.token),),
    ).fetchone()
    if row is None:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_capabilities'"
        ).fetchone():
            delegated = conn.execute(
                "SELECT c.*,s.scope FROM task_capabilities c JOIN sessions s ON s.id=c.session_id "
                "WHERE c.token_hash=? AND c.revoked=0 AND c.expires_at>aitm_clock()",
                (token_hash(handle.token),),
            ).fetchone()
            if delegated is not None:
                return TaskSession(delegated["session_id"], delegated["reference"],
                                   json.loads(delegated["task_ids"])), Scope(**json.loads(delegated["scope"]))
        raise AccessDenied("Invalid or revoked session capability")
    return row["id"], Scope(**json.loads(row["scope"]))


def register(conn, scope: Scope, resume_evidence=None, resume_ref=None) -> SessionHandle:
    if not isinstance(scope, Scope) or not all(
        isinstance(x, str) and x.strip() for x in vars(scope).values()
    ):
        raise ValidationError("Complete trusted session scope is required")
    encoded = record(scope)
    row = conn.execute("SELECT id FROM sessions WHERE scope=?", (encoded,)).fetchone()
    token = secrets.token_urlsafe(32)
    if row:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_capabilities'").fetchone() and conn.execute(
            "SELECT 1 FROM task_capabilities WHERE session_id=? LIMIT 1", (row["id"],)
        ).fetchone():
            raise AccessDenied("A superseded session cannot be renewed or reset")
        if (
            resume_evidence is None
            or resume_evidence.kind != "session_resume"
            or resume_evidence.scope != scope
            or resume_evidence.subject != row["id"]
        ):
            raise EvidenceRequired("Known sessions require trusted resume or handoff")
        if conn.execute(
            "SELECT 1 FROM authority_receipts WHERE proof_ref=?", (resume_ref,)
        ).fetchone():
            raise EvidenceRequired("Session resume evidence was already consumed")
        conn.execute("INSERT INTO authority_receipts VALUES(?,?)", (resume_ref, row["id"]))
        conn.execute(
            "UPDATE sessions SET token_hash=?,revoked=0 WHERE id=?",
            (token_hash(token), row["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO sessions(id,scope,token_hash) VALUES(?,?,?)",
            (str(uuid4()), encoded, token_hash(token)),
        )
    return SessionHandle(token)
