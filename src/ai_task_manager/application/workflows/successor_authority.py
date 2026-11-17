"""Fresh task authority is separate from the retained revoked session token."""

import json
from dataclasses import asdict

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, IdempotencyConflict
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS, Scope, SessionHandle
from ai_task_manager.storage import attempts, issue_claims, sessions, work


def admit(core, old_handle, new_handle, authorization, publish):
    """Publish guarded lineage before exposing a new finite task capability.

    Workflow pointers and lineage use the existing workflow transaction. The
    Core capability becomes usable only after that publication commits. A lost
    Core commit may leave a recorded successor without a usable capability;
    exact retry completes the same admission instead of publishing another run.
    """
    from .recovery import SuccessorAuthorization

    if (type(authorization) is not SuccessorAuthorization
            or type(old_handle) is not SessionHandle or type(new_handle) is not SessionHandle
            or not callable(publish)):
        raise AccessDenied("Typed current successor authority and opaque handles required")
    c, a = core.context, authorization
    request = asdict(a)
    fingerprint = work.fingerprint({"authorization": request, "token": sessions.token_hash(new_handle.token)})
    with c.db.transaction() as conn:
        old = conn.execute("SELECT id,scope FROM sessions WHERE token_hash=?",
                           (sessions.token_hash(old_handle.token),)).fetchone()
        if old is None or Scope(**json.loads(old["scope"])) != a.scope:
            raise AccessDenied("Original Core session identity is unavailable")
        sid = old["id"]
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_capabilities'").fetchone()
        if exists:
            prior = conn.execute("SELECT * FROM task_capabilities WHERE reference=?", (a.reference,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint or prior["session_id"] != sid:
                    raise IdempotencyConflict("Successor capability has different immutable inputs")
                return prior["workflow_ref"]
        proof = c.proof(a.reference, "workflow_successor", a.scope, a.task_id)
        if work.fingerprint(proof.payload) != work.fingerprint(request) or proof.expires_at != a.expires_at or c.clock() >= a.expires_at:
            raise AccessDenied("Fresh signed authority differs from the successor request")
        task = work.get(conn, sid, a.task_id)
        c.check(task, a.task_version, a.decision_revision)
        decision = c.decision(conn, task, a.scope, a.decision_ref, "In Progress")
        if decision.expires_at is None or decision.expires_at < a.expires_at:
            raise AccessDenied("Successor deadline exceeds the current explicit decision")
        if (task.human_hold or task.delivered_once or not task.issue_id or not task.queued_once
                or task.sync_state != "confirmed" or task.execution_state in ACTIVE_ATTEMPTS
                or task.observed_status not in {"Todo", "In Progress", "On Hold"}
                or tuple(unit for unit in task.required_work if unit not in task.completed_work) != a.remaining_work):
            raise EvidenceRequired("Current task is not a reconciled unfinished predecessor")
        issue_claims.require_open(conn, a.scope, task.issue_id)
        allowed = (a.task_id, *a.consumers)
        if len(set(allowed)) != len(allowed):
            raise AccessDenied("Successor task consumers must be distinct")
        for task_id in allowed:
            selected = work.get(conn, sid, task_id)
            if selected.human_hold or selected.delivered_once or selected.sync_state != "confirmed":
                raise EvidenceRequired("A required consumer is held, delivered or uncertain")
        marks = ",".join("?" for _ in allowed)
        for row in conn.execute(f"SELECT data,active FROM attempts WHERE session_id=? AND task_id IN ({marks})", (sid, *allowed)):
            attempt = attempts.attempt_from(json.loads(row["data"]))
            if row["active"] or attempt.state in ACTIVE_ATTEMPTS:
                raise EvidenceRequired("Active or unresolved attempts prevent successor admission")
        if conn.execute(f"SELECT 1 FROM outbox WHERE session_id=? AND task_id IN ({marks}) AND state NOT IN ('confirmed','failed') LIMIT 1", (sid, *allowed)).fetchone():
            raise EvidenceRequired("Unreconciled provider effects prevent successor admission")
        if conn.execute("SELECT 1 FROM resource_leases r JOIN attempts a ON a.id=r.attempt_id WHERE a.session_id=? LIMIT 1", (sid,)).fetchone():
            raise EvidenceRequired("Unsettled resource claims prevent successor admission")
        # This is an additive explicit authority enrollment, never legacy backfill.
        conn.execute("CREATE TABLE IF NOT EXISTS task_capabilities("
                     "reference TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,"
                     "session_id TEXT NOT NULL REFERENCES sessions(id),task_ids TEXT NOT NULL,"
                     "workflow_ref TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,"
                     "expires_at REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0)")
        if not exists:
            from ai_task_manager.storage.scheduling_schema import trigger_sql
            from ai_task_manager.storage.publication_schema import writer_trigger

            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(trigger_sql("task_capabilities", operation))
                conn.execute(writer_trigger("task_capabilities", operation))
        for prior in conn.execute("SELECT reference,task_ids FROM task_capabilities WHERE session_id=? AND revoked=0", (sid,)).fetchall():
            subjects = set(json.loads(prior["task_ids"]))
            if subjects & set(allowed):
                if subjects != set(allowed):
                    raise EvidenceRequired("Another task capability has unrelated consumers")
                conn.execute("UPDATE task_capabilities SET revoked=1 WHERE reference=?", (prior["reference"],))
        reference = publish(conn, task)
        conn.execute("INSERT INTO task_capabilities VALUES(?,?,?,?,?,?,?,0)",
                     (a.reference,sessions.token_hash(new_handle.token),sid,json.dumps(allowed),
                      reference,fingerprint,a.expires_at))
        return reference
