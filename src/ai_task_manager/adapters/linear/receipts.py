"""Private provider observations; this journal never writes core task tables."""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import Evidence, Scope

from .errors import LinearError, ScopeMismatch


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


KINDS = frozenset(
    {
        "provider_status",
        "provider_ingress",
        "ingress_applied",
        "external_observation",
        "outbound_absent",
        "external_prose_readback",
        "provider_progress",
    }
)
SCHEMA = """
CREATE TABLE metadata(version INTEGER NOT NULL);
INSERT INTO metadata VALUES(3);
CREATE TABLE proofs(ref TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
 subject TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE principals(ref TEXT PRIMARY KEY, scope TEXT NOT NULL);
CREATE TABLE deliveries(id TEXT PRIMARY KEY, scope TEXT NOT NULL, delivery_id TEXT NOT NULL,
 raw_digest TEXT NOT NULL, semantic_digest TEXT NOT NULL, payload TEXT NOT NULL,
 ingress_ref TEXT NOT NULL, state TEXT NOT NULL, owner TEXT, lease_until REAL,
 checkpoint TEXT NOT NULL DEFAULT '{}', error TEXT,
 UNIQUE(scope,delivery_id), UNIQUE(scope,semantic_digest));
CREATE TABLE sends(id TEXT PRIMARY KEY, scope TEXT NOT NULL, operation_id TEXT NOT NULL,
 attempt INTEGER NOT NULL, state TEXT NOT NULL, snapshot TEXT, error TEXT, candidate_id TEXT,
 UNIQUE(scope,operation_id,attempt));
CREATE TABLE history(id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,
 stage TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE ignored_deliveries(scope TEXT NOT NULL, delivery_id TEXT NOT NULL,
 raw_digest TEXT NOT NULL, reason TEXT NOT NULL, received_at REAL NOT NULL,
 UNIQUE(scope,raw_digest));
"""


class ProviderJournal:
    def __init__(self, path, *, clock=time.time):
        self.path = str(path)
        self.clock = clock
        if self.path == ":memory:":
            raise LinearError("durable_journal_required")
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
        with self.transaction() as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not tables:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                try:
                    versions = conn.execute("SELECT version FROM metadata").fetchall()
                    if len(versions) != 1 or versions[0][0] not in {1, 2, 3}:
                        raise LinearError("unsupported_provider_schema")
                    if versions[0][0] == 1:
                        conn.execute("ALTER TABLE sends ADD COLUMN candidate_id TEXT")
                        conn.execute("UPDATE metadata SET version=2")
                    if versions[0][0] in {1, 2}:
                        conn.execute(
                            "CREATE TABLE ignored_deliveries(scope TEXT NOT NULL, "
                            "delivery_id TEXT NOT NULL, raw_digest TEXT NOT NULL, "
                            "reason TEXT NOT NULL, "
                            "received_at REAL NOT NULL, UNIQUE(scope,raw_digest))"
                        )
                        conn.execute("UPDATE metadata SET version=3")
                except sqlite3.DatabaseError as exc:
                    raise LinearError("foreign_provider_store") from exc

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _proof(conn, kind, scope, subject, payload):
        if kind not in KINDS:
            raise EvidenceRequired("Provider issuer does not own this proof kind")
        ref = f"linear-proof:{uuid4()}"
        conn.execute(
            "INSERT INTO proofs VALUES(?,?,?,?,?)",
            (ref, kind, encoded(asdict(scope)), subject, encoded(payload)),
        )
        return ref

    def verify_evidence(self, ref):
        if not isinstance(ref, str) or not ref.startswith("linear-proof:"):
            raise EvidenceRequired("Unknown provider proof issuer")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM proofs WHERE ref=?", (ref,)).fetchone()
            if not row or row["kind"] not in KINDS:
                raise EvidenceRequired("Provider proof is missing or has an invalid issuer kind")
            payload = json.loads(row["payload"])
            return Evidence(
                row["kind"],
                Scope(**json.loads(row["scope"])),
                row["subject"],
                payload,
                expires_at=payload["observed_at"] + 30
                if row["kind"] == "external_prose_readback"
                else None,
            )

    def prose_readback_receipt(self, scope, task, snapshot):
        payload = {
            "issue_id": snapshot.id,
            "source_revision": snapshot.revision,
            "title": snapshot.title,
            "body": snapshot.body,
            "status": snapshot.status,
            "assigned_to_owner": snapshot.assignee_id == scope.principal,
            "observed_at": self.clock(),
        }
        with self.transaction() as conn:
            return self._proof(conn, "external_prose_readback", scope, task.id, payload)

    def progress_receipt(self, scope, task, publication_id, snapshot):
        payload = {
            "publication_id": publication_id,
            "comment_id": snapshot.id,
            "issue_id": snapshot.issue.id,
            "author_id": snapshot.author_id,
            "body_digest": digest({"body": snapshot.body}),
            "task_prose_digest": task.prose_digest,
        }
        with self.transaction() as conn:
            return self._proof(conn, "provider_progress", scope, task.id, payload)

    def bind_principal(self, verified_binding, scope):
        expected = verified_binding.expected
        if (
            scope.provider != "linear"
            or scope.principal != expected.viewer_id
            or scope.workspace != expected.workspace_id
            or scope.project != expected.project_id
            or not scope.logical_session
        ):
            raise ScopeMismatch()
        # The logical session comes from trusted host registration, never an HTTP request.
        ref = f"linear-principal:{uuid4()}"
        with self.transaction() as conn:
            conn.execute("INSERT INTO principals VALUES(?,?)", (ref, encoded(asdict(scope))))
        return ref

    def verify_principal(self, ref):
        if not isinstance(ref, str) or not ref.startswith("linear-principal:"):
            raise AccessDenied("Unknown provider principal issuer")
        with self.transaction() as conn:
            row = conn.execute("SELECT scope FROM principals WHERE ref=?", (ref,)).fetchone()
            if not row:
                raise AccessDenied("Verified provider principal is missing")
            return Scope(**json.loads(row[0]))

    def accept_delivery(self, scope, delivery_id, raw_body, payload):
        scope_text = encoded(asdict(scope))
        raw_digest = hashlib.sha256(raw_body).hexdigest()
        semantic = digest({k: v for k, v in payload.items() if k != "webhookTimestamp"})
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM deliveries WHERE scope=? AND delivery_id=?",
                (scope_text, delivery_id),
            ).fetchone()
            if old and old["raw_digest"] != raw_digest:
                raise LinearError("delivery_payload_conflict")
            if old:
                return dict(old), False
            old = conn.execute(
                "SELECT * FROM deliveries WHERE scope=? AND semantic_digest=?",
                (scope_text, semantic),
            ).fetchone()
            if old:
                return dict(old), False
            ref = self._proof(
                conn, "provider_ingress", scope, delivery_id, {"payload_digest": digest(payload)}
            )
            receipt_id = str(uuid4())
            conn.execute(
                "INSERT INTO deliveries(id,scope,delivery_id,raw_digest,semantic_digest,"
                "payload,ingress_ref,state) "
                "VALUES(?,?,?,?,?,?,?,'received')",
                (receipt_id, scope_text, delivery_id, raw_digest, semantic, encoded(payload), ref),
            )
            return dict(
                conn.execute("SELECT * FROM deliveries WHERE id=?", (receipt_id,)).fetchone()
            ), True

    def ignore_delivery(self, scope, delivery_id, raw_body, reason, now):
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ignored_deliveries VALUES(?,?,?,?,?)",
                (
                    encoded(asdict(scope)),
                    delivery_id,
                    hashlib.sha256(raw_body).hexdigest(),
                    reason,
                    now,
                ),
            )

    def ignored(self, scope):
        with self.transaction() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM ignored_deliveries WHERE scope=?", (encoded(asdict(scope)),)
                )
            ]

    def record_wake_failure(self, subject, error_kind):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO history(subject,stage,detail) VALUES(?,?,?)",
                (subject, "wake_failed", error_kind),
            )

    def status_receipt(self, scope, task, operation, snapshot):
        from ai_task_manager.domains.work.models import valid_display_identifier

        if not valid_display_identifier(snapshot.identifier):
            raise LinearError("invalid_display_identifier", uncertain=True)
        if (
            snapshot.status != task.desired_status
            or snapshot.title != task.title
            or snapshot.body != task.body
            or snapshot.assignee_id != scope.principal
            or (task.issue_id and task.issue_id != snapshot.id)
        ):
            raise LinearError("readback_conflict", uncertain=True)
        payload = {
            "operation_id": operation.id,
            "status": snapshot.status,
            "title": snapshot.title,
            "body": snapshot.body,
            "representation": task.representation,
            "prose_digest": task.prose_digest,
            "issue_id": snapshot.id,
            "display_identifier": snapshot.identifier,
            "source_revision": snapshot.revision,
        }
        with self.transaction() as conn:
            return self._proof(conn, "provider_status", scope, task.id, payload)

    def external_receipt(self, scope, task, snapshot, event, authorized_actors):
        actor = event.get("actor") or {}
        authorized = actor.get("type") == "user" and actor.get("id") in authorized_actors
        payload = {
            "issue_id": snapshot.id,
            "status": snapshot.status,
            "source_revision": snapshot.revision,
            "title": snapshot.title,
            "body": snapshot.body,
            "actor_id": actor.get("id"),
            "actor_kind": "human" if actor.get("type") == "user" else "integration",
            "actor_authorized": authorized,
            "assigned_to_owner": snapshot.assignee_id == scope.principal,
            "reason": "Human requested On Hold in Linear" if snapshot.status == "On Hold" else "",
        }
        with self.transaction() as conn:
            return self._proof(conn, "external_observation", scope, task.id, payload)

    def application_receipt(self, scope, delivery_id, payload):
        with self.transaction() as conn:
            return self._proof(
                conn, "ingress_applied", scope, delivery_id, {"payload_digest": digest(payload)}
            )

    def unsent_receipt(self, scope, operation):
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM sends WHERE scope=? AND operation_id=?",
                (encoded(asdict(scope)), operation.id),
            ).fetchone():
                raise EvidenceRequired("A recorded send cannot be declared absent")
            return self._proof(
                conn, "outbound_absent", scope, operation.id, {"safe_to_retry": True}
            )

    def removal_receipt(self, scope, task, event, authorized_actors):
        actor = event.get("actor") or {}
        payload = {
            "issue_id": task.issue_id,
            "status": "Removed",
            "source_revision": event["data"]["updatedAt"],
            "title": task.title,
            "body": task.body,
            "actor_id": actor.get("id"),
            "actor_kind": "human" if actor.get("type") == "user" else "integration",
            "actor_authorized": actor.get("type") == "user"
            and actor.get("id") in authorized_actors,
            "assigned_to_owner": False,
            "reason": "Registered issue removed from Linear",
        }
        from .models import revision

        payload["source_revision"] = revision(payload["source_revision"])
        with self.transaction() as conn:
            return self._proof(conn, "external_observation", scope, task.id, payload)

    def scope_removal_receipt(self, scope, task, metadata):
        from .models import revision

        if metadata.get("id") != task.issue_id:
            raise ScopeMismatch()
        payload = {
            "issue_id": task.issue_id,
            "status": "Outside managed scope",
            "source_revision": revision(metadata["updatedAt"]),
            "title": task.title,
            "body": task.body,
            "actor_kind": "integration",
            "actor_authorized": False,
            "assigned_to_owner": False,
            "scope_digest": metadata["scopeDigest"],
            "reason": "Registered issue left its configured scope",
        }
        with self.transaction() as conn:
            return self._proof(conn, "external_observation", scope, task.id, payload)

    def claim_delivery(self, scope, owner, now, lease_seconds=30):
        if not owner or not 0 < lease_seconds <= 300:
            raise LinearError("invalid_receiver_lease")
        with self.transaction() as conn:
            scope_text = encoded(asdict(scope))
            conn.execute(
                "UPDATE deliveries SET state='received',owner=NULL "
                "WHERE scope=? AND state='leased' AND lease_until<=?",
                (scope_text, now),
            )
            row = conn.execute(
                "SELECT * FROM deliveries WHERE scope=? AND state='received' "
                "ORDER BY rowid LIMIT 1",
                (scope_text,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE deliveries SET state='leased',owner=?,lease_until=? WHERE id=?",
                (owner, now + lease_seconds, row["id"]),
            )
            return dict(
                conn.execute("SELECT * FROM deliveries WHERE id=?", (row["id"],)).fetchone()
            )

    def checkpoint(self, receipt_id, owner, values, now):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE id=?", (receipt_id,)).fetchone()
            if (
                not row
                or row["state"] != "leased"
                or row["owner"] != owner
                or row["lease_until"] <= now
            ):
                raise LinearError("lost_receiver_lease")
            data = {**json.loads(row["checkpoint"]), **values}
            conn.execute(
                "UPDATE deliveries SET checkpoint=? WHERE id=?", (encoded(data), receipt_id)
            )
            return data

    def finish_delivery(self, receipt_id, owner, state, now, error=None):
        if state not in {"applied", "ignored", "conflict"}:
            raise LinearError("invalid_application_outcome")
        with self.transaction() as conn:
            result = conn.execute(
                "UPDATE deliveries SET state=?,error=? WHERE id=? AND owner=? "
                "AND state='leased' AND lease_until>?",
                (state, error, receipt_id, owner, now),
            )
            if result.rowcount != 1:
                raise LinearError("lost_receiver_lease")
            conn.execute(
                "INSERT INTO history(subject,stage,detail) VALUES(?,?,?)",
                (receipt_id, state, error or ""),
            )

    def release_delivery(self, receipt_id, owner, now):
        with self.transaction() as conn:
            result = conn.execute(
                "UPDATE deliveries SET state='received',owner=NULL,lease_until=NULL "
                "WHERE id=? AND owner=? AND state='leased' AND lease_until>?",
                (receipt_id, owner, now),
            )
            if result.rowcount != 1:
                raise LinearError("lost_receiver_lease")

    def retry_delivery(self, scope, receipt_id):
        with self.transaction() as conn:
            result = conn.execute(
                "UPDATE deliveries SET state='received',owner=NULL,lease_until=NULL "
                "WHERE id=? AND scope=? AND state='conflict'",
                (receipt_id, encoded(asdict(scope))),
            )
            if result.rowcount != 1:
                raise LinearError("conflict_not_available")

    def deliveries(self, scope):
        with self.transaction() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM deliveries WHERE scope=? ORDER BY rowid",
                    (encoded(asdict(scope)),),
                )
            ]

    def begin_send(self, scope, operation, candidate_id=None):
        with self.transaction() as conn:
            scope_text = encoded(asdict(scope))
            old = conn.execute(
                "SELECT * FROM sends WHERE scope=? AND operation_id=? AND attempt=?",
                (scope_text, operation.id, operation.attempt_count),
            ).fetchone()
            if old:
                raise LinearError("send_already_recorded", uncertain=True)
            send_id = str(uuid4())
            conn.execute(
                "INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)",
                (
                    send_id,
                    scope_text,
                    operation.id,
                    operation.attempt_count,
                    "sending",
                    None,
                    None,
                    candidate_id,
                ),
            )
            return send_id

    def finish_send(self, send_id, state, snapshot=None, error=None):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sends SET state=?,snapshot=COALESCE(?,snapshot),error=? WHERE id=?",
                (state, encoded(asdict(snapshot)) if snapshot else None, error, send_id),
            )
            conn.execute(
                "INSERT INTO history(subject,stage,detail) VALUES(?,?,?)",
                (send_id, state, error or ""),
            )

    def sends(self, scope):
        with self.transaction() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sends WHERE scope=? ORDER BY rowid", (encoded(asdict(scope)),)
                )
            ]

    def is_echo(self, scope, snapshot):
        for row in self.sends(scope):
            if row["snapshot"] and row["state"] in {"readback", "confirmed"}:
                old = json.loads(row["snapshot"])
                if old == asdict(snapshot):
                    return True
        return False

    def latest_snapshot(self, scope, issue_id):
        snapshots = [
            json.loads(row["snapshot"])
            for row in self.sends(scope)
            if row["snapshot"] and row["state"] in {"readback", "confirmed"}
        ]
        for row in self.deliveries(scope):
            checkpoint = json.loads(row["checkpoint"])
            if row["state"] == "applied" and checkpoint.get("snapshot"):
                snapshots.append(checkpoint["snapshot"])
        snapshots = [item for item in snapshots if item["id"] == issue_id]
        return max(snapshots, key=lambda item: item["revision"]) if snapshots else None
