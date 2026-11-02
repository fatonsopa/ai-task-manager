"""Own operation facts and callback intents; never task or provider authority."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from ai_task_manager.config.files import digest, private_path, strict_json
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    IdempotencyConflict,
    NotFound,
    SchemaError,
    StaleVersion,
)

from .contracts import encode, token


class Journal:
    def __init__(self, path, *, faults=None):
        self.path, self.faults = Path(path).absolute(), faults
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if self.path.parent.is_symlink() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise AccessDenied("Journal requires an owned private directory")
        private_path(self.path, existing=False)
        try:
            with self.path.open("xb"):
                os.chmod(self.path, 0o600)
        except FileExistsError:
            pass
        self.identity = self._identity()
        with self._transaction() as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            expected = {"ai_meta", "ai_operations", "ai_events", "ai_callbacks", "ai_replays"}
            if tables and tables != expected:
                raise SchemaError("Journal cannot open a foreign store")
            if not tables:
                for sql in (
                    "CREATE TABLE ai_meta(version INTEGER NOT NULL, store_id TEXT NOT NULL)",
                    (
                        "CREATE TABLE ai_operations(ref TEXT PRIMARY KEY, "
                        "operation_key TEXT UNIQUE NOT NULL, "
                        "binding TEXT NOT NULL, "
                        "fingerprint TEXT NOT NULL, "
                        "version INTEGER NOT NULL, "
                        "facts TEXT NOT NULL, "
                        "registration_receipt TEXT NOT NULL)"
                    ),
                    (
                        "CREATE TABLE ai_events(operation_ref TEXT NOT NULL, "
                        "version INTEGER NOT NULL, "
                        "kind TEXT NOT NULL, "
                        "payload TEXT NOT NULL, "
                        "PRIMARY KEY(operation_ref,version))"
                    ),
                    (
                        "CREATE TABLE ai_callbacks(operation_ref TEXT NOT NULL, "
                        "sequence INTEGER NOT NULL, "
                        "digest TEXT NOT NULL, "
                        "envelope TEXT NOT NULL, "
                        "state TEXT NOT NULL, "
                        "PRIMARY KEY(operation_ref,sequence))"
                    ),
                    (
                        "CREATE TABLE ai_replays(operation_ref TEXT NOT NULL, "
                        "key TEXT NOT NULL, "
                        "fingerprint TEXT NOT NULL, "
                        "receipt TEXT NOT NULL, "
                        "PRIMARY KEY(operation_ref,key))"
                    ),
                ):
                    conn.execute(sql)
                conn.execute("INSERT INTO ai_meta VALUES(1,?)", (str(uuid4()),))
            rows = conn.execute("SELECT version,store_id FROM ai_meta").fetchall()
            if len(rows) != 1 or rows[0][0] != 1:
                raise SchemaError("Unsupported operation journal format")
            try:
                if str(UUID(rows[0][1])) != rows[0][1]:
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                raise SchemaError("Invalid operation journal identity") from None
            self.store_id = rows[0][1]

    def _identity(self):
        value = private_path(self.path).stat()
        return value.st_dev, value.st_ino

    @contextmanager
    def _transaction(self):
        if self._identity() != self.identity:
            raise AccessDenied("Journal file identity changed")
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self._identity() != self.identity:
                raise AccessDenied("Journal file identity changed")
            if hasattr(self, "store_id"):
                rows = conn.execute("SELECT version,store_id FROM ai_meta").fetchall()
                if len(rows) != 1 or tuple(rows[0]) != (1, self.store_id):
                    raise AccessDenied("Operation journal binding changed")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _fault(self, stage):
        if self.faults is not None:
            self.faults.hit(stage)

    @staticmethod
    def _operation(conn, ref):
        row = conn.execute("SELECT * FROM ai_operations WHERE ref=?", (token(ref),)).fetchone()
        if row is None:
            raise NotFound("Registered operation unavailable")
        return {
            "ref": row["ref"],
            "binding": strict_json(row["binding"]),
            "fingerprint": row["fingerprint"],
            "version": row["version"],
            "facts": strict_json(row["facts"]),
        }

    def current(self, ref):
        with self._transaction() as conn:
            return self._operation(conn, ref)

    def for_attempt(self, attempt_id):
        with self._transaction() as conn:
            return [
                self._operation(conn, r[0])
                for r in conn.execute("SELECT ref FROM ai_operations")
                if strict_json(
                    conn.execute(
                        "SELECT binding FROM ai_operations WHERE ref=?", (r[0],)
                    ).fetchone()[0]
                )["attempt_id"]
                == attempt_id
            ]

    def register(self, binding, fingerprint):
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT * FROM ai_operations WHERE operation_key=?", (binding["operation_key"],)
            ).fetchone()
            if old is not None:
                if old["fingerprint"] != fingerprint:
                    raise IdempotencyConflict("Operation key has different registered input")
                return strict_json(old["registration_receipt"])
            ref = "ai-operation:" + str(uuid4())
            result = {"ref": ref, "version": 0, "kind": "registered", "facts": {}}
            conn.execute(
                "INSERT INTO ai_operations VALUES(?,?,?,?,?,?,?)",
                (
                    ref,
                    binding["operation_key"],
                    encode(binding),
                    fingerprint,
                    0,
                    "{}",
                    encode(result),
                ),
            )
            conn.execute("INSERT INTO ai_events VALUES(?,0,'registered','{}')", (ref,))
            return result

    @staticmethod
    def _replay(conn, ref, key, fingerprint):
        row = conn.execute(
            "SELECT fingerprint,receipt FROM ai_replays WHERE operation_ref=? AND key=?",
            (ref, token(key)),
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise IdempotencyConflict("Idempotency key has different input")
            return strict_json(row[1])
        return None

    def replay(self, ref, key, request):
        with self._transaction() as conn:
            self._operation(conn, ref)
            return self._replay(conn, ref, key, digest(request))

    def commit(
        self, ref, expected_version, key, kind, facts, payload=None, callbacks=(), *, request=None
    ):
        if type(expected_version) is not int:
            raise StaleVersion("Expected version must be an integer")
        fingerprint = digest(
            request if request is not None else [kind, facts, payload, list(callbacks)]
        )
        with self._transaction() as conn:
            replay = self._replay(conn, ref, key, fingerprint)
            if replay is not None:
                return replay
            operation = self._operation(conn, ref)
            if expected_version != operation["version"]:
                raise StaleVersion("Operation version changed")
            version = expected_version + 1
            conn.execute(
                "UPDATE ai_operations SET facts=?,version=? WHERE ref=? AND version=?",
                (encode(facts), version, ref, expected_version),
            )
            self._fault("after_state")
            conn.execute(
                "INSERT INTO ai_events VALUES(?,?,?,?)",
                (ref, version, token(kind), encode(payload)),
            )
            self._fault("after_event")
            for cb in callbacks:
                conn.execute(
                    "INSERT INTO ai_callbacks VALUES(?,?,?,?,'queued')",
                    (ref, cb["sequence"], digest(cb), encode(cb)),
                )
            self._fault("after_callbacks")
            result = {"ref": ref, "version": version, "kind": kind, "facts": facts}
            conn.execute(
                "INSERT INTO ai_replays VALUES(?,?,?,?)", (ref, key, fingerprint, encode(result))
            )
            self._fault("before_commit")
        self._fault("after_commit")
        return result

    def callback(self, ref, sequence):
        with self._transaction() as conn:
            self._operation(conn, ref)
            row = conn.execute(
                "SELECT * FROM ai_callbacks WHERE operation_ref=? AND sequence=?", (ref, sequence)
            ).fetchone()
            if row is None:
                raise NotFound("Callback intent unavailable")
            return {
                "envelope": strict_json(row["envelope"]),
                "state": row["state"],
                "digest": row["digest"],
            }

    def callback_transition(
        self, ref, sequence, old_state, new_state, key, evidence_ref="absent", *, claim=False
    ):
        fingerprint = digest([sequence, new_state, evidence_ref])
        with self._transaction() as conn:
            replay = self._replay(conn, ref, key, fingerprint)
            if replay is not None:
                return (replay, False) if claim else replay
            operation = self._operation(conn, ref)
            changed = conn.execute(
                "UPDATE ai_callbacks SET state=? WHERE operation_ref=? AND sequence=? AND state=?",
                (new_state, ref, sequence, old_state),
            ).rowcount
            if changed != 1:
                raise StaleVersion("Callback state changed")
            version = operation["version"] + 1
            conn.execute("UPDATE ai_operations SET version=? WHERE ref=?", (version, ref))
            conn.execute(
                "INSERT INTO ai_events VALUES(?,?,?,?)",
                (
                    ref,
                    version,
                    "callback_receipt",
                    encode(
                        {"sequence": sequence, "stage": new_state, "evidence_ref": evidence_ref}
                    ),
                ),
            )
            result = {"ref": ref, "sequence": sequence, "stage": new_state, "version": version}
            conn.execute(
                "INSERT INTO ai_replays VALUES(?,?,?,?)", (ref, key, fingerprint, encode(result))
            )
            return (result, True) if claim else result

    def events(self, ref):
        with self._transaction() as conn:
            self._operation(conn, ref)
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM ai_events WHERE operation_ref=? ORDER BY version", (ref,)
                )
            ]
