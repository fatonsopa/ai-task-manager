from ai_task_manager.domains.work.errors import EvidenceRequired, ValidationError
from ai_task_manager.domains.work.models import ReceiptStage
from ai_task_manager.storage import events, sessions


class Notifications:
    def __init__(self, context):
        self.c = context

    def list_events(self, handle, after=0):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return events.list_events(conn, sid, after)

    def acknowledge(self, handle, event_id, stage, proof_ref, operation_key):
        try:
            stage = ReceiptStage(stage)
        except ValueError as exc:
            raise ValidationError("Unknown notification receipt stage") from exc
        payload = {
            "command": "receipt",
            "event_id": event_id,
            "stage": stage,
            "proof_ref": proof_ref,
        }

        def action(conn, sid, scope):
            events.get(conn, sid, event_id)
            proof = self.c.proof(proof_ref, f"notification_{stage}", scope, event_id)
            if proof.payload.get("target_session_id") != sid:
                raise EvidenceRequired("Notification proof belongs to another target session")
            events.receipt(conn, event_id, stage, proof_ref)
            return {"stage": stage}

        return self.c.mutation(handle, operation_key, payload, action)["stage"]

    def cursor(self, handle):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            row = conn.execute("SELECT sequence FROM cursors WHERE session_id=?", (sid,)).fetchone()
            return row[0] if row else 0

    def advance_cursor(self, handle, sequence):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            maximum = conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM events WHERE session_id=?",
                (sid,),
            ).fetchone()[0]
            old = conn.execute("SELECT sequence FROM cursors WHERE session_id=?", (sid,)).fetchone()
            if type(sequence) is not int or not (old[0] if old else 0) <= sequence <= maximum:
                raise ValidationError("Cursor must advance within this session's event stream")
            conn.execute(
                "INSERT INTO cursors VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET sequence=?",
                (sid, sequence, sequence),
            )

    def receipts(self, handle, event_id):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            events.get(conn, sid, event_id)
            return {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT stage,proof_ref FROM receipts WHERE event_id=?", (event_id,)
                )
            }
