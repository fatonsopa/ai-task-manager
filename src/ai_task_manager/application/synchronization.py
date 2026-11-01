from ai_task_manager.domains.work.errors import EvidenceRequired, ValidationError
from ai_task_manager.storage import outbox, sessions
from ai_task_manager.storage.work import fingerprint


class Synchronization:
    """Durable adapter boundary. This component performs no network operations."""

    def __init__(self, context):
        self.c = context

    def list_operations(self, handle):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return [
                outbox.get(conn, sid, row[0])
                for row in conn.execute(
                    "SELECT id FROM outbox WHERE session_id=? ORDER BY rowid", (sid,)
                ).fetchall()
            ]

    def claim(self, handle, adapter_id, lease_seconds=30):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return outbox.claim(conn, sid, adapter_id, self.c.clock(), lease_seconds)

    def record_outcome(self, handle, operation_id, adapter_id, attempt, state, detail):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            outbox.outcome(
                conn,
                sid,
                operation_id,
                adapter_id,
                attempt,
                state,
                detail,
                self.c.clock(),
            )

    def reconcile_retry(self, handle, operation_id, proof_ref):
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            operation = outbox.get(conn, sid, operation_id)
            proof = self.c.proof(proof_ref, "outbound_absent", scope, operation_id)
            if (
                operation.state not in {"failed", "uncertain"}
                or proof.payload.get("safe_to_retry") is not True
            ):
                raise EvidenceRequired(
                    "Retry requires reconciliation proving no remote side effect"
                )
            conn.execute(
                "UPDATE outbox SET state='pending',lease_owner=NULL,lease_until=NULL WHERE id=?",
                (operation_id,),
            )

    def ingest(self, handle, delivery_id, payload, ingress_ref):
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            proof = self.c.proof(ingress_ref, "provider_ingress", scope, delivery_id)
            if proof.payload.get("payload_digest") != fingerprint(payload):
                raise EvidenceRequired(
                    "Ingress evidence does not match the authenticated payload"
                )
            return outbox.ingest(conn, sid, delivery_id, payload)

    def inbound(self, handle):
        import json

        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return [
                {
                    "delivery_id": row["delivery_id"],
                    "payload": json.loads(row["payload"]),
                    "state": row["state"],
                }
                for row in conn.execute(
                    "SELECT * FROM inbox WHERE session_id=? ORDER BY rowid", (sid,)
                )
            ]

    def mark_applied(self, handle, delivery_id, proof_ref):
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            proof = self.c.proof(proof_ref, "ingress_applied", scope, delivery_id)
            row = conn.execute(
                "SELECT digest FROM inbox WHERE session_id=? AND delivery_id=?",
                (sid, delivery_id),
            ).fetchone()
            if not row or proof.payload.get("payload_digest") != row[0]:
                raise ValidationError(
                    "Applied receipt must match a durable inbound delivery"
                )
            conn.execute(
                "UPDATE inbox SET state='applied' WHERE session_id=? AND delivery_id=?",
                (sid, delivery_id),
            )
