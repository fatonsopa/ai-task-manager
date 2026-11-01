"""Durable, scoped progress publication intents and retained attempts."""

import time
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.domains.work.errors import IdempotencyConflict, NotFound

from .errors import LinearError
from .receipts import digest, encoded


class ProgressStore:
    def __init__(self, journal, *, clock=time.time):
        self.journal, self.clock = journal, clock
        with journal.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS progress_metadata(version INTEGER NOT NULL)")
            versions = conn.execute("SELECT version FROM progress_metadata").fetchall()
            if not versions:
                conn.execute("INSERT INTO progress_metadata VALUES(1)")
                conn.execute(
                    "CREATE TABLE progress_publications(id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                    "task_id TEXT NOT NULL, operation_key TEXT UNIQUE NOT NULL, "
                    "fingerprint TEXT NOT NULL, "
                    "payload TEXT NOT NULL, state TEXT NOT NULL, owner TEXT, lease_until REAL, "
                    "candidate_id TEXT, comment_id TEXT, error TEXT, created_at REAL NOT NULL, "
                    "sent_at REAL, observed_at REAL, confirmed_at REAL)"
                )
                conn.execute(
                    "CREATE TABLE progress_history(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "publication_id TEXT NOT NULL, state TEXT NOT NULL, "
                    "recorded_at REAL NOT NULL, detail TEXT)"
                )
            elif len(versions) != 1 or versions[0][0] != 1:
                raise LinearError("unsupported_progress_schema")

    def submit(self, scope, task_id, operation_key, payload):
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise IdempotencyConflict("Publication operation identity is required")
        scope_text, fingerprint = encoded(asdict(scope)), digest(payload)
        with self.journal.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM progress_publications WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if old:
                if (
                    old["scope"] != scope_text
                    or old["task_id"] != task_id
                    or old["fingerprint"] != fingerprint
                ):
                    raise IdempotencyConflict("Publication identity has a different scoped request")
                return dict(old)
            publication_id = str(uuid4())
            conn.execute(
                "INSERT INTO progress_publications(id,scope,task_id,operation_key,"
                "fingerprint,payload,"
                "state,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                (
                    publication_id,
                    scope_text,
                    task_id,
                    operation_key,
                    fingerprint,
                    encoded(payload),
                    self.clock(),
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM progress_publications WHERE id=?", (publication_id,)
                ).fetchone()
            )

    def get(self, scope, publication_id):
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM progress_publications WHERE id=? AND scope=?",
                (publication_id, encoded(asdict(scope))),
            ).fetchone()
            if row is None:
                raise NotFound("Publication is not available in this session")
            return dict(row)

    def claim(self, scope, publication_id, owner, candidate_enabled):
        now = self.clock()
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM progress_publications WHERE id=? AND scope=?",
                (publication_id, encoded(asdict(scope))),
            ).fetchone()
            if row is None:
                raise NotFound("Publication is not available in this session")
            if row["state"] == "leased" and row["lease_until"] <= now:
                conn.execute(
                    "UPDATE progress_publications SET state='uncertain',"
                    "error='lease expired' WHERE id=?",
                    (publication_id,),
                )
                conn.execute(
                    "INSERT INTO progress_history(publication_id,state,recorded_at,detail) "
                    "VALUES(?,?,?,?)",
                    (publication_id, "uncertain", now, "lease expired"),
                )
                return None
            if row["state"] != "pending":
                return None
            conn.execute(
                "UPDATE progress_publications SET state='leased',owner=?,lease_until=?,"
                "candidate_id=? WHERE id=?",
                (owner, now + 120, publication_id if candidate_enabled else None, publication_id),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM progress_publications WHERE id=?", (publication_id,)
                ).fetchone()
            )

    def mark_sent(self, scope, publication_id, owner):
        with self.journal.transaction() as conn:
            result = conn.execute(
                "UPDATE progress_publications SET sent_at=? WHERE id=? AND scope=? "
                "AND state='leased' AND owner=? AND lease_until>?",
                (self.clock(), publication_id, encoded(asdict(scope)), owner, self.clock()),
            )
            if result.rowcount != 1:
                raise LinearError("lost_progress_lease")

    def record_response(self, scope, publication_id, owner, comment_id):
        with self.journal.transaction() as conn:
            result = conn.execute(
                "UPDATE progress_publications SET comment_id=?,observed_at=? "
                "WHERE id=? AND scope=? AND state='leased' AND owner=?",
                (comment_id, self.clock(), publication_id, encoded(asdict(scope)), owner),
            )
            if result.rowcount != 1:
                raise LinearError("lost_progress_lease")

    def finish(self, scope, publication_id, state, error=None):
        if state not in {"confirmed", "failed", "uncertain"}:
            raise LinearError("invalid_progress_outcome")
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM progress_publications WHERE id=? AND scope=?",
                (publication_id, encoded(asdict(scope))),
            ).fetchone()
            if row is None:
                raise NotFound("Publication is not available in this session")
            if row["state"] == "confirmed" and state != "confirmed":
                return dict(
                    conn.execute(
                        "SELECT * FROM progress_publications WHERE id=?", (publication_id,)
                    ).fetchone()
                )
            conn.execute(
                "UPDATE progress_publications SET state=?,error=?,confirmed_at=? WHERE id=?",
                (state, error, self.clock() if state == "confirmed" else None, publication_id),
            )
            conn.execute(
                "INSERT INTO progress_history(publication_id,state,recorded_at,detail) "
                "VALUES(?,?,?,?)",
                (publication_id, state, self.clock(), error),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM progress_publications WHERE id=?", (publication_id,)
                ).fetchone()
            )

    def retry_unsent(self, scope, publication_id):
        with self.journal.transaction() as conn:
            result = conn.execute(
                "UPDATE progress_publications SET state='pending',owner=NULL,lease_until=NULL "
                "WHERE id=? AND scope=? AND state IN ('failed','uncertain') AND sent_at IS NULL",
                (publication_id, encoded(asdict(scope))),
            )
            if result.rowcount != 1:
                raise LinearError("progress_effect_not_proven_absent")
