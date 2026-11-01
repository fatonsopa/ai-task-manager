import json
from uuid import uuid4

from ai_task_manager.domains.work.errors import (
    EvidenceRequired,
    IdempotencyConflict,
    NotFound,
    ValidationError,
)
from ai_task_manager.domains.work.models import OutboundOperation

from .database import encode
from .work import fingerprint


def from_row(row) -> OutboundOperation:
    return OutboundOperation(
        row["id"],
        row["session_id"],
        row["task_id"],
        row["kind"],
        json.loads(row["payload"]),
        row["state"],
        row["attempt_count"],
        row["lease_owner"],
        row["lease_until"],
        row["error"],
        row["fenced_by"],
    )


def get(conn, session_id: str, operation_id: str) -> OutboundOperation:
    row = conn.execute(
        "SELECT * FROM outbox WHERE id=? AND session_id=?", (operation_id, session_id)
    ).fetchone()
    if row is None:
        raise NotFound("Outbound operation is not available in this session")
    return from_row(row)


def append(conn, session_id: str, task_id: str, kind: str, payload: dict) -> str:
    operation_id = str(uuid4())
    conn.execute(
        "INSERT INTO outbox(id,session_id,task_id,kind,payload,state) VALUES(?,?,?,?,?,'pending')",
        (operation_id, session_id, task_id, kind, encode(payload)),
    )
    return operation_id


def claim(
    conn, session_id: str, owner: str, now: float, lease_seconds: float, task_id=None
) -> OutboundOperation | None:
    if not owner or not 0 < lease_seconds <= 300:
        raise ValidationError("A bounded lease and adapter identity are required")
    expired = conn.execute(
        "SELECT id,attempt_count FROM outbox WHERE session_id=? AND state='leased' "
        "AND lease_until<=?",
        (session_id, now),
    ).fetchall()
    for row in expired:
        conn.execute(
            "UPDATE outbox SET state='uncertain',error='lease expired' WHERE id=?",
            (row["id"],),
        )
        conn.execute(
            "INSERT INTO outbound_attempts(operation_id,attempt,outcome,detail) VALUES(?,?,?,?)",
            (row["id"], row["attempt_count"], "uncertain", "lease expired"),
        )
    row = conn.execute(
        "SELECT id FROM outbox WHERE session_id=? AND state='pending' "
        "AND fenced_by IS NULL "
        "AND (? IS NULL OR task_id=?) ORDER BY rowid LIMIT 1",
        (session_id, task_id, task_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE outbox SET state='leased',attempt_count=attempt_count+1,"
        "lease_owner=?,lease_until=? "
        "WHERE id=?",
        (owner, now + lease_seconds, row[0]),
    )
    return get(conn, session_id, row[0])


def outcome(
    conn,
    session_id: str,
    operation_id: str,
    owner: str,
    attempt: int,
    state: str,
    detail: str,
    now: float,
):
    operation = get(conn, session_id, operation_id)
    if (
        operation.state != "leased"
        or operation.lease_owner != owner
        or operation.attempt_count != attempt
        or operation.lease_until <= now
    ):
        raise ValidationError("Outbound lease is no longer owned by this attempt")
    if state not in {"uncertain", "failed"} or not detail.strip():
        raise ValidationError("Unconfirmed attempts require an explicit failure or uncertainty")
    conn.execute("UPDATE outbox SET state=?,error=? WHERE id=?", (state, detail, operation_id))
    conn.execute(
        "INSERT INTO outbound_attempts(operation_id,attempt,outcome,detail) VALUES(?,?,?,?)",
        (operation_id, attempt, state, detail),
    )


def confirm(conn, session_id: str, operation_id: str, proof_ref: str):
    operation = get(conn, session_id, operation_id)
    if operation.fenced_by:
        raise EvidenceRequired("A newer external observation fenced this operation")
    if operation.state == "confirmed":
        return
    conn.execute("UPDATE outbox SET state='confirmed',error=NULL WHERE id=?", (operation_id,))
    conn.execute(
        "INSERT INTO outbound_attempts(operation_id,attempt,outcome,detail) VALUES(?,?,?,?)",
        (operation_id, operation.attempt_count, "confirmed", proof_ref),
    )


def ingest(conn, session_id: str, delivery_id: str, payload: dict) -> bool:
    if not delivery_id:
        raise ValidationError("Provider delivery identity is required")
    digest = fingerprint(payload)
    row = conn.execute(
        "SELECT digest FROM inbox WHERE session_id=? AND delivery_id=?",
        (session_id, delivery_id),
    ).fetchone()
    if row:
        if row[0] != digest:
            raise IdempotencyConflict("Provider reused a delivery identity with changed content")
        return False
    conn.execute(
        "INSERT INTO inbox(session_id,delivery_id,digest,payload) VALUES(?,?,?,?)",
        (session_id, delivery_id, digest, encode(payload)),
    )
    return True
