import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_task_manager.domains.work.errors import SchemaError


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record(value: Any) -> str:
    return encode(asdict(value))


_SCHEMA = """
CREATE TABLE metadata(version INTEGER NOT NULL);
INSERT INTO metadata VALUES(2);
CREATE TABLE sessions(id TEXT PRIMARY KEY, scope TEXT UNIQUE NOT NULL,
 token_hash TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE authority_receipts(proof_ref TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES sessions(id));
CREATE TABLE tasks(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 issue_id TEXT, version INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE issue_bindings(identity_key TEXT PRIMARY KEY,
 task_id TEXT UNIQUE NOT NULL REFERENCES tasks(id));
CREATE INDEX task_sessions ON tasks(session_id);
CREATE TABLE attempts(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 session_id TEXT NOT NULL REFERENCES sessions(id), host TEXT NOT NULL,
 active INTEGER NOT NULL, data TEXT NOT NULL);
CREATE UNIQUE INDEX one_active_attempt ON attempts(task_id) WHERE active=1;
CREATE TABLE resource_leases(kind TEXT NOT NULL, resource_id TEXT NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES attempts(id), PRIMARY KEY(kind,resource_id));
CREATE TABLE capacity(host TEXT PRIMARY KEY, host_limit INTEGER NOT NULL CHECK(host_limit>0),
 project_limit INTEGER NOT NULL CHECK(project_limit>0));
CREATE TABLE operations(key TEXT PRIMARY KEY, session_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE checkpoints(id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
 kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE events(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 sequence INTEGER NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL,
 payload TEXT NOT NULL, UNIQUE(session_id, sequence));
CREATE TABLE receipts(event_id TEXT NOT NULL REFERENCES events(id), stage TEXT NOT NULL,
 proof_ref TEXT NOT NULL, PRIMARY KEY(event_id, stage));
CREATE TABLE cursors(session_id TEXT PRIMARY KEY REFERENCES sessions(id),
 sequence INTEGER NOT NULL);
CREATE TABLE outbox(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, lease_owner TEXT,
 lease_until REAL, error TEXT, fenced_by TEXT);
CREATE TABLE outbound_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL
 REFERENCES outbox(id), attempt INTEGER NOT NULL, outcome TEXT NOT NULL, detail TEXT);
CREATE TABLE inbox(session_id TEXT NOT NULL REFERENCES sessions(id), delivery_id TEXT NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'received',
 PRIMARY KEY(session_id, delivery_id));
CREATE TABLE external_observations(id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(id),
 source_revision TEXT NOT NULL, digest TEXT NOT NULL, proof_ref TEXT NOT NULL,
 payload TEXT NOT NULL,
 state TEXT NOT NULL, UNIQUE(task_id,source_revision,digest));
"""


class Database:
    """A connection per unit of work; SQLite serializes independent writers."""

    @classmethod
    def open_existing(cls, path, *, guard):
        """Use an enrolled current store without initialization or migration."""
        value = cls.__new__(cls)
        value.path, value.guard = str(Path(path).absolute()), guard
        value._active_transactions = set()
        value._existing_only = True
        with value.transaction() as conn:
            if [tuple(r) for r in conn.execute("SELECT version FROM metadata")] != [(5,)]:
                raise SchemaError("Current original task store required")
        return value

    def __init__(self, path: str | Path, *, guard=None):
        self.path = str(path)
        self.guard = guard
        self._active_transactions = set()
        if self.path == ":memory:":
            raise SchemaError("Use a disposable file for shared transactional storage")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self.guard:
                self.guard(conn)
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not tables:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                try:
                    rows = conn.execute("SELECT version FROM metadata").fetchall()
                except sqlite3.DatabaseError as exc:
                    raise SchemaError("Database is not an owned task store") from exc
                if len(rows) != 1 or rows[0][0] not in {1, 2, 3, 4, 5}:
                    raise SchemaError("Unsupported task store schema")
                if rows[0][0] < 4:
                    from ai_task_manager.storage.scheduling_schema import require_quiescent

                    require_quiescent(conn)
                if rows[0][0] < 5:
                    from ai_task_manager.storage.core_migration_image import preserve

                    preserve(conn, self.path, rows[0][0])
                if rows[0][0] == 1:
                    conn.execute("ALTER TABLE outbox ADD COLUMN fenced_by TEXT")
                    conn.execute(
                        "CREATE TABLE external_observations(id TEXT PRIMARY KEY, "
                        "task_id TEXT NOT NULL REFERENCES tasks(id), "
                        "source_revision TEXT NOT NULL, "
                        "digest TEXT NOT NULL, proof_ref TEXT NOT NULL, payload TEXT NOT NULL, "
                        "state TEXT NOT NULL, UNIQUE(task_id,source_revision,digest))"
                    )
                    conn.execute("UPDATE metadata SET version=2")
            version = conn.execute("SELECT version FROM metadata").fetchone()[0]
            if version == 2:
                conn.execute("CREATE TABLE IF NOT EXISTS store_identity(id TEXT PRIMARY KEY)")
                if not conn.execute("SELECT 1 FROM store_identity").fetchone():
                    conn.execute("INSERT INTO store_identity VALUES(?)", (str(uuid4()),))
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS issue_lifecycle(identity_key TEXT PRIMARY KEY, "
                    "delivered INTEGER NOT NULL,terminal INTEGER NOT NULL,source TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS existing_admissions("
                    "task_id TEXT PRIMARY KEY REFERENCES tasks(id),"
                    " source_revision TEXT NOT NULL,digest TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS existing_checks(task_id TEXT PRIMARY KEY "
                    "REFERENCES tasks(id),version INTEGER NOT NULL,revision INTEGER NOT NULL,"
                    "source_ref TEXT NOT NULL,checked_at REAL NOT NULL)"
                )
                from ai_task_manager.storage.issue_claims import seed_lifecycle

                seed_lifecycle(conn)
                conn.execute("UPDATE metadata SET version=3")
            from ai_task_manager.storage.scheduling_schema import migrate

            migrate(conn)
            from ai_task_manager.storage.publication_schema import migrate as migrate_publication

            migrate_publication(conn)
            if self.guard:
                self.guard(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def identity(conn):
        rows = conn.execute("SELECT id FROM store_identity").fetchall()
        if len(rows) != 1:
            raise SchemaError("Task store identity is invalid")
        from uuid import UUID

        try:
            if str(UUID(rows[0][0])) != rows[0][0]:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise SchemaError("Task store identity is invalid") from exc
        return rows[0][0]

    def connect(self) -> sqlite3.Connection:
        existing = getattr(self, "_existing_only", False)
        conn = sqlite3.connect(
            Path(self.path).as_uri() + "?mode=rw" if existing else self.path,
            uri=existing,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        import time

        conn.create_function("aitm_clock", 0, getattr(self, "clock", time.time))
        from ai_task_manager.storage.scheduling_schema import register

        register(conn)
        from ai_task_manager.storage.publication_schema import register as register_publication

        register_publication(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def require_transaction(self, connection):
        """Accept only a currently leased connection from this exact store object."""
        if connection not in self._active_transactions or not connection.in_transaction:
            raise SchemaError("Connection does not belong to this active Core transaction")

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self.guard:
                self.guard(conn)
            versions = conn.execute("SELECT version FROM metadata").fetchall()
            if len(versions) != 1 or versions[0][0] not in {1, 2, 3, 4, 5}:
                raise SchemaError("Unsupported task store schema")
            if versions[0][0] >= 4:
                from ai_task_manager.storage.scheduling_schema import validate

                validate(conn)
            if versions[0][0] == 5:
                from ai_task_manager.storage.publication_schema import (
                    validate as validate_publication,
                )

                validate_publication(conn)
            self._active_transactions.add(conn)
            yield conn
            if self.guard:
                self.guard(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._active_transactions.discard(conn)
            conn.close()
