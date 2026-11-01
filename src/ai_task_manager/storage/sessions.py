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
