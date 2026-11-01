import json
from uuid import uuid4

from ai_task_manager.domains.work.errors import NotFound, ValidationError
from ai_task_manager.domains.work.models import Event, ReceiptStage

from .database import encode


def append(conn, session_id: str, task_id: str, kind: str, payload: dict) -> Event:
    sequence = conn.execute(
        "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE session_id=?",
        (session_id,),
    ).fetchone()[0]
    event = Event(str(uuid4()), session_id, sequence, task_id, kind, payload)
    conn.execute(
        "INSERT INTO events VALUES(?,?,?,?,?,?)",
        (event.id, session_id, sequence, task_id, kind, encode(payload)),
    )
    conn.execute(
        "INSERT INTO receipts VALUES(?,?,?)",
        (event.id, ReceiptStage.QUEUED, "local-transaction"),
    )
    return event


def get(conn, session_id: str, event_id: str) -> Event:
    row = conn.execute(
        "SELECT * FROM events WHERE session_id=? AND id=?", (session_id, event_id)
    ).fetchone()
    if row is None:
        raise NotFound("Event is not available in this session")
    return Event(
        row["id"],
        session_id,
        row["sequence"],
        row["task_id"],
        row["kind"],
        json.loads(row["payload"]),
    )


def list_events(conn, session_id: str, after: int = 0) -> list[Event]:
    if type(after) is not int or after < 0:
        raise ValidationError("Event cursor must be a nonnegative integer")
    rows = conn.execute(
        "SELECT id FROM events WHERE session_id=? AND sequence>? ORDER BY sequence",
        (session_id, after),
    ).fetchall()
    return [get(conn, session_id, row[0]) for row in rows]


def receipt(conn, event_id: str, stage: ReceiptStage, proof_ref: str):
    stages = list(ReceiptStage)
    previous = conn.execute(
        "SELECT stage,proof_ref FROM receipts WHERE event_id=?", (event_id,)
    ).fetchall()
    seen = {row["stage"]: row["proof_ref"] for row in previous}
    if stage in seen:
        if seen[stage] != proof_ref:
            raise ValidationError("Receipt already has different evidence")
        return
    if stage == ReceiptStage.QUEUED or stages[stages.index(stage) - 1] not in seen:
        raise ValidationError("Notification receipt cannot skip a stage")
    conn.execute("INSERT INTO receipts VALUES(?,?,?)", (event_id, stage, proof_ref))
