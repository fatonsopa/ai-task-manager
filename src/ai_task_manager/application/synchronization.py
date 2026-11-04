from ai_task_manager.domains.work.errors import EvidenceRequired, ValidationError
from ai_task_manager.domains.work.transitions import human_hold_required
from ai_task_manager.storage import outbox, sessions, work
from ai_task_manager.storage.work import fingerprint


class Synchronization:
    """Durable adapter boundary. This component performs no network operations."""

    def __init__(self, context):
        self.c = context

    def record_progress_publication(self, handle, task_id, proof_ref):
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            task = work.get(conn, sid, task_id)
            proof = self.c.proof(proof_ref, "provider_progress", scope, task.id)
            data = proof.payload
            if (
                data.get("issue_id") != task.issue_id
                or data.get("author_id") != scope.principal
                or data.get("task_prose_digest") != task.prose_digest
                or not data.get("comment_id")
                or not data.get("body_digest")
                or not data.get("publication_id")
            ):
                raise EvidenceRequired("Progress publication receipt does not match this task")
            key = f"provider-progress:{data['publication_id']}"
            payload = {"command": "provider_progress", "task_id": task.id, **data}
            prior = work.replay(conn, sid, key, payload)
            if prior is not None:
                return prior
            self.c.changed(conn, task, "provider_progress_publication_confirmed", data)
            result = {"publication_id": data["publication_id"], "state": "provider_confirmed"}
            work.remember(conn, sid, key, payload, result)
            return result

    def list_operations(self, handle):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            return [
                outbox.get(conn, sid, row[0])
                for row in conn.execute(
                    "SELECT id FROM outbox WHERE session_id=? ORDER BY rowid", (sid,)
                ).fetchall()
            ]

    def claim(self, handle, adapter_id, lease_seconds=30, task_id=None):
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            if task_id is not None:
                work.get(conn, sid, task_id)
            return outbox.claim(conn, sid, adapter_id, self.c.clock(), lease_seconds, task_id)

    def authorizes_hold_publication(self, handle, operation_id):
        """Verify the current human hold intent without authorizing any work or retry."""
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            operation = outbox.get(conn, sid, operation_id)
            task = work.get(conn, sid, operation.task_id)
            allowed_causes = (
                {"dequeue", "review_decision"}
                if task.desired_status == "Backlog"
                else {"hold", "review_decision"}
            )
            if (
                not task.human_hold
                or not human_hold_required(task.desired_status)
                or operation.kind != "set_status"
                or operation.fenced_by
                or operation.state == "confirmed"
                or operation.payload.get("cause") not in allowed_causes
                or operation.payload.get("task_version") != task.version
                or operation.payload.get("decision_revision") != task.decision_revision
                or operation.payload.get("desired_status") != task.desired_status
            ):
                return False
            human = self.c.proof(task.decision_ref, "human_decision", scope, task.id).payload
            if (
                human.get("actor") != "human"
                or human.get("hold") is not True
                or human.get("target") != task.desired_status
                or human.get("previous_revision") != task.decision_revision - 1
                or human.get("prose_digest") != task.prose_digest
            ):
                return False
            self.c.decision(
                conn,
                task,
                scope,
                operation.payload.get("decision_ref"),
                task.desired_status,
                human=True,
            )
            return True

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
                operation.fenced_by
                or operation.state not in {"failed", "uncertain"}
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
                raise EvidenceRequired("Ingress evidence does not match the authenticated payload")
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
                raise ValidationError("Applied receipt must match a durable inbound delivery")
            conn.execute(
                "UPDATE inbox SET state='applied' WHERE session_id=? AND delivery_id=?",
                (sid, delivery_id),
            )
