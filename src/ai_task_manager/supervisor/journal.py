"""Runtime-owned facts and intents; task state remains in Core."""

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import RuntimeRefused, RuntimeUncertain


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class RuntimeJournal:
    @classmethod
    def open_existing(cls, path, *, clock=time.time):
        from ai_task_manager.adapters.workflow_control.recovery import physical

        value = cls.__new__(cls)
        value.path, value.clock = Path(path).absolute(), clock
        value._existing_binding = physical(path)
        with value.transaction():
            pass
        return value

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path).absolute()
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.transaction() as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and "runtime_metadata" not in tables:
                raise RuntimeRefused("A foreign or core database cannot be a runtime journal")
            conn.execute("CREATE TABLE IF NOT EXISTS runtime_metadata(version INTEGER NOT NULL)")
            row = conn.execute("SELECT version FROM runtime_metadata").fetchall()
            if not row:
                conn.execute("INSERT INTO runtime_metadata VALUES(1)")
            elif len(row) != 1 or row[0][0] != 1:
                raise RuntimeRefused("Unsupported runtime journal")
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_attempts(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_effects(
                attempt_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(attempt_id,kind))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_observations(
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, kind TEXT NOT NULL,
                data TEXT NOT NULL, digest TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS runtime_receipts(
                ref TEXT PRIMARY KEY, data TEXT NOT NULL, signature TEXT NOT NULL)""")
        os.chmod(self.path, 0o600)

    @contextmanager
    def transaction(self):
        binding = getattr(self, "_existing_binding", None)
        conn = sqlite3.connect(
            self.path.as_uri() + "?mode=rw" if binding else self.path,
            uri=binding is not None,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("BEGIN IMMEDIATE")
            if binding:
                from ai_task_manager.adapters.workflow_control.recovery import _probe

                _probe(conn, binding, "runtime")
            yield conn
            if binding:
                _probe(conn, binding, "runtime")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim(self, attempt, scope, *, deadline: float):
        if not math.isfinite(deadline) or deadline <= self.clock():
            raise RuntimeRefused("Runtime deadline must be finite and in the future")
        data = {
            "attempt_id": attempt.id,
            "task_id": attempt.task_id,
            "worker_id": f"runtime:{uuid4()}",
            "host": attempt.host,
            "scope": scope,
            "created_at": self.clock(),
            "deadline": deadline,
        }
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM runtime_attempts WHERE id=?", (attempt.id,)).fetchone():
                raise RuntimeUncertain(
                    "Runtime intent already exists; reconcile instead of relaunch"
                )
            conn.execute(
                "INSERT INTO runtime_attempts VALUES(?,?,?)",
                (attempt.id, attempt.task_id, canonical(data)),
            )
        return data

    def attempt(self, attempt_id):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT data FROM runtime_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        if not row:
            raise RuntimeRefused("Runtime attempt is not registered")
        return json.loads(row[0])

    def intend(self, attempt_id, kind, data):
        self.attempt(attempt_id)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT data FROM runtime_effects WHERE attempt_id=? AND kind=?", (attempt_id, kind)
            ).fetchone()
            if row:
                raise RuntimeUncertain(f"Existing {kind} intent needs reconciliation")
            conn.execute(
                "INSERT INTO runtime_effects VALUES(?,?,?)", (attempt_id, kind, canonical(data))
            )

    def effect(self, attempt_id, kind):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT data FROM runtime_effects WHERE attempt_id=? AND kind=?", (attempt_id, kind)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def observe(self, attempt_id, kind, data):
        self.attempt(attempt_id)
        identity = str(uuid4())
        stored = {"observed_at": self.clock(), **data}
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime_observations VALUES(?,?,?,?,?)",
                (identity, attempt_id, kind, canonical(stored), digest(stored)),
            )
        return identity

    def observation(self, identity, *, attempt_id=None, kind=None):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_observations WHERE id=?", (identity,)
            ).fetchone()
        if not row:
            raise RuntimeRefused("Runtime observation is missing")
        data = json.loads(row["data"])
        if (
            digest(data) != row["digest"]
            or (attempt_id is not None and row["attempt_id"] != attempt_id)
            or (kind is not None and row["kind"] != kind)
        ):
            raise RuntimeRefused("Runtime observation was changed or belongs to another owner")
        return {"id": identity, "attempt_id": row["attempt_id"], "kind": row["kind"], **data}

    def observations(self, attempt_id, kind):
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM runtime_observations WHERE attempt_id=? AND kind=? ORDER BY rowid",
                (attempt_id, kind),
            ).fetchall()
        return [self.observation(row[0], attempt_id=attempt_id, kind=kind) for row in rows]
