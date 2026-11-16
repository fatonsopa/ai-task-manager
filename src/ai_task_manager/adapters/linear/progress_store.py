"""Durable, scoped progress publication intents and retained attempts."""

import json
import time
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.domains.work.errors import IdempotencyConflict, NotFound

from .errors import LinearError
from .receipts import digest, encoded


class ProgressStore:
    @classmethod
    def open_existing(cls, journal, *, clock=time.time):
        value = cls.__new__(cls)
        value.journal, value.clock = journal, clock
        with journal.transaction() as conn:
            if [tuple(row) for row in conn.execute("SELECT version FROM progress_metadata")] != [
                (2,)
            ]:
                raise LinearError("unsupported_progress_schema")
        return value

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
            elif len(versions) != 1 or versions[0][0] not in {1, 2}:
                raise LinearError("unsupported_progress_schema")
            if not versions or versions[0][0] == 1:
                for name, declaration in {
                    "decision_state": "TEXT",
                    "label_state": "TEXT",
                    "label_response": "TEXT",
                    "resolution_key": "TEXT",
                    "resolution": "TEXT",
                    "effect_owner": "TEXT",
                    "effect_lease": "REAL",
                }.items():
                    conn.execute(
                        "ALTER TABLE progress_publications ADD COLUMN " + name + " " + declaration
                    )
                conn.execute(
                    "CREATE UNIQUE INDEX progress_resolution_key ON "
                    "progress_publications(scope,resolution_key) WHERE "
                    "resolution_key IS NOT NULL"
                )
                conn.execute("UPDATE progress_metadata SET version=2")

    @staticmethod
    def retained(journal, scope, task_id):
        """Read existing publication history without creating or migrating it."""
        with journal.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_metadata'"
            ).fetchone():
                return []
            if [tuple(row) for row in conn.execute("SELECT version FROM progress_metadata")] != [
                (2,)
            ]:
                raise LinearError("unsupported_progress_schema")
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM progress_publications WHERE scope=? AND task_id=? "
                    "ORDER BY rowid",
                    (encoded(asdict(scope)), task_id),
                )
            ]

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
            if "decision" in payload:
                anchor = self._anchor(conn, scope_text, payload["issue_id"])
                if anchor and anchor["effect_owner"]:
                    raise LinearError("decision_issue_effect_busy", uncertain=True)
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
            if "decision" in payload:
                conn.execute(
                    "UPDATE progress_publications SET "
                    "decision_state='open',label_state='pending' WHERE id=?",
                    (publication_id,),
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

    @staticmethod
    def _anchor(conn, scope_text, issue_id):
        return conn.execute(
            "SELECT * FROM progress_publications WHERE scope=? AND decision_state IS NOT NULL "
            "AND json_extract(payload,'$.issue_id')=? ORDER BY rowid LIMIT 1",
            (scope_text, issue_id),
        ).fetchone()

    def decision_claim(self, scope, publication_id, owner, *, intent=None):
        intent = intent or {"kind": "publication"}

        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM progress_publications WHERE id=? AND scope=?",
                (publication_id, encoded(asdict(scope))),
            ).fetchone()
            if row is None or row["decision_state"] is None:
                raise NotFound("Decision request is unavailable in this session")
            issue_id = json.loads(row["payload"])["issue_id"]
            anchor = self._anchor(conn, encoded(asdict(scope)), issue_id)
            if anchor["effect_owner"]:
                raise LinearError("decision_issue_effect_busy", uncertain=True)
            other = conn.execute(
                "SELECT id FROM progress_publications WHERE scope=? AND decision_state IS NOT NULL "
                "AND json_extract(payload,'$.issue_id')=? AND label_state IN"
                " ('sent','uncertain') AND id!=? LIMIT 1",
                (encoded(asdict(scope)), issue_id, publication_id),
            ).fetchone()
            if other:
                raise LinearError("decision_label_effect_uncertain", uncertain=True)
            conn.execute(
                "UPDATE progress_publications SET effect_owner=?,effect_lease=? WHERE id=?",
                (owner, self.clock() + 120, anchor["id"]),
            )
            conn.execute(
                "INSERT INTO progress_history(publication_id,state,recorded_at,detail) "
                "VALUES(?,?,?,?)",
                (
                    publication_id,
                    "effect-owner",
                    self.clock(),
                    encoded(
                        {
                            "scope": encoded(asdict(scope)),
                            "anchor": anchor["id"],
                            "owner": owner,
                            "fingerprint": row["fingerprint"],
                            "intent": intent,
                        }
                    ),
                ),
            )
            return anchor["id"]

    def release_completed_effect(self, scope, publication_id):
        """Release a forward-bound completed claim; missing owner history stays protected."""
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM progress_publications WHERE scope=? AND id=?",
                (encoded(asdict(scope)), publication_id),
            ).fetchone()
            if row is None or row["state"] != "confirmed" or row["label_state"] != "confirmed":
                return False
            anchor = self._anchor(
                conn, encoded(asdict(scope)), json.loads(row["payload"])["issue_id"]
            )
            if anchor is None or not anchor["effect_owner"]:
                return False
            owners = [
                json.loads(r[0])
                for r in conn.execute(
                    "SELECT detail FROM progress_history WHERE publication_id=? "
                    "AND state='effect-owner'",
                    (publication_id,),
                )
                if json.loads(r[0]).get("owner") == anchor["effect_owner"]
            ]
            if len(owners) != 1:
                return False
            fact = owners[0]
            if any(
                fact.get(k) != value
                for k, value in {
                    "scope": encoded(asdict(scope)),
                    "anchor": anchor["id"],
                    "fingerprint": row["fingerprint"],
                }.items()
            ):
                return False
            intent = fact.get("intent", {})
            complete = (intent == {"kind": "publication"} and row["decision_state"] == "open") or (
                intent.get("kind") == "resolution"
                and row["decision_state"] == "resolved"
                and intent.get("operation_key") == row["resolution_key"]
                and encoded(intent.get("resolution")) == row["resolution"]
            )
            if not complete:
                return False
            changed = conn.execute(
                "UPDATE progress_publications SET effect_owner=NULL,effect_lease=NULL "
                "WHERE id=? AND effect_owner=?",
                (anchor["id"], fact["owner"]),
            ).rowcount
            if changed:
                conn.execute(
                    "INSERT INTO progress_history(publication_id,state,recorded_at,detail) "
                    "VALUES(?,?,?,?)",
                    (publication_id, "effect-owner-reconciled", self.clock(), encoded(fact)),
                )
            return bool(changed)

    def decision_release(self, anchor_id, owner):
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE progress_publications SET "
                "effect_owner=NULL,effect_lease=NULL WHERE id=? AND "
                "effect_owner=?",
                (anchor_id, owner),
            )

    def label_stage(self, scope, publication_id, state, response=None):
        with self.journal.transaction() as conn:
            result = conn.execute(
                "UPDATE progress_publications SET "
                "label_state=?,label_response=COALESCE(?,label_response) "
                "WHERE id=? AND scope=? AND decision_state IS NOT NULL",
                (
                    state,
                    encoded(response) if response is not None else None,
                    publication_id,
                    encoded(asdict(scope)),
                ),
            )
            if result.rowcount != 1:
                raise NotFound("Decision request is unavailable")
            conn.execute(
                "INSERT INTO "
                "progress_history(publication_id,state,recorded_at,detail) "
                "VALUES(?,?,?,?)",
                (publication_id, "label:" + state, self.clock(), None),
            )

    def record_resolution(self, scope, publication_id, operation_key, resolution):
        with self.journal.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM progress_publications WHERE id=? AND scope=?",
                (publication_id, encoded(asdict(scope))),
            ).fetchone()
            if row is None:
                raise NotFound("Decision request is unavailable")
            if row["resolution_key"]:
                if row["resolution_key"] != operation_key or row["resolution"] != encoded(
                    resolution
                ):
                    raise IdempotencyConflict(
                        "Decision resolution has another original source or key"
                    )
                return
            conn.execute(
                "UPDATE progress_publications SET "
                "decision_state='resolving',resolution_key=?,resolution=?,label_state='pending',label_response=NULL"
                " WHERE id=?",
                (operation_key, encoded(resolution), publication_id),
            )

    def open_decisions(self, scope, issue_id):
        with self.journal.transaction() as conn:
            return conn.execute(
                "SELECT count(*) FROM progress_publications WHERE scope=? "
                "AND decision_state='open' AND "
                "json_extract(payload,'$.issue_id')=?",
                (encoded(asdict(scope)), issue_id),
            ).fetchone()[0]

    def finish_resolution(self, scope, publication_id):
        with self.journal.transaction() as conn:
            conn.execute(
                "UPDATE progress_publications SET decision_state='resolved' "
                "WHERE id=? AND scope=? AND decision_state='resolving' AND "
                "label_state='confirmed'",
                (publication_id, encoded(asdict(scope))),
            )
