"""Workflow intent and ownership history, never a second task-status database."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.config.files import private_path
from ai_task_manager.domains.checks.models import canonical, fingerprint
from ai_task_manager.domains.work.errors import AccessDenied, IdempotencyConflict, SchemaError
from ai_task_manager.storage.coherence import StoreBinding, probe_connection


class WorkflowStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.stat().st_mode & 0o077:
            raise AccessDenied("Workflow storage requires a private directory")
        private_path(self.path, existing=False)
        try:
            with self.path.open("xb"):
                self.path.chmod(0o600)
        except FileExistsError:
            pass
        info = private_path(self.path).stat()
        self.identity = info.st_dev, info.st_ino
        with self.transaction() as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not tables:
                conn.execute("CREATE TABLE workflow_meta(version INTEGER,store_id TEXT)")
                conn.execute("INSERT INTO workflow_meta VALUES(1,?)", (str(uuid4()),))
                conn.execute(
                    "CREATE TABLE runs(id TEXT PRIMARY KEY,scope TEXT,task_id TEXT,"
                    "operation_key TEXT,"
                    "body TEXT,owner TEXT,UNIQUE(scope,task_id),UNIQUE(scope,operation_key))"
                )
                conn.execute(
                    "CREATE TABLE steps(run_id TEXT,name TEXT,payload TEXT,result TEXT,"
                    "PRIMARY KEY(run_id,name))"
                )
                conn.execute(
                    "CREATE TABLE lineage(resource TEXT PRIMARY KEY,task_id TEXT,"
                    "attempt_id TEXT,body TEXT)"
                )
            elif tables != {"workflow_meta", "runs", "steps", "lineage"}:
                raise SchemaError("Not an owned workflow store")
            rows = conn.execute("SELECT version,store_id FROM workflow_meta").fetchall()
            if len(rows) != 1 or rows[0][0] != 1:
                raise SchemaError("Unsupported workflow store")
            self.store_id = rows[0][1]

    @contextmanager
    def transaction(self):
        info = private_path(self.path).stat()
        if (info.st_dev, info.st_ino) != self.identity:
            raise AccessDenied("Workflow file identity changed")
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            if hasattr(self, "store_id"):
                probe_connection(
                    conn, StoreBinding(self.path, *self.identity, "workflow_meta", 1, self.store_id)
                )
            yield conn
            if hasattr(self, "store_id"):
                probe_connection(
                    conn, StoreBinding(self.path, *self.identity, "workflow_meta", 1, self.store_id)
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, scope, task, plan, key, runtime_digest=None):
        body = {
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "decision_revision": task.decision_revision,
            "plan_id": plan.id,
            "plan_digest": plan.digest,
            "runtime_digest": runtime_digest,
            "attempt_id": None,
        }
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM runs WHERE scope=? AND (task_id=? OR operation_key=?)",
                (canonical(asdict(scope)), task.id, key),
            ).fetchone()
            if old:
                prior = json.loads(old["body"])
                if (
                    old["operation_key"],
                    prior["task_id"],
                    prior["prose_digest"],
                    prior["plan_digest"],
                    prior["runtime_digest"],
                ) != (key, task.id, task.prose_digest, plan.digest, runtime_digest):
                    raise IdempotencyConflict("Task already has a different workflow or plan")
                return old["id"]
            ref = "workflow:" + str(uuid4())
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,NULL)",
                (ref, canonical(asdict(scope)), task.id, key, canonical(body)),
            )
            return ref

    def find(self, scope, task_id, operation_key):
        """Find exact historical admission without granting another effect."""
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id,task_id,operation_key FROM runs "
                "WHERE scope=? AND (task_id=? OR operation_key=?)",
                (canonical(asdict(scope)), task_id, operation_key),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1 or (rows[0]["task_id"], rows[0]["operation_key"]) != (
                task_id,
                operation_key,
            ):
                raise IdempotencyConflict("Workflow task or original operation key changed")
            return rows[0]["id"]

    def read(self, reference, scope):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id=? AND scope=?", (reference, canonical(asdict(scope)))
            ).fetchone()
            if row is None:
                raise AccessDenied("Scoped workflow is unavailable")
            return dict(
                json.loads(row["body"]),
                id=reference,
                owner=row["owner"],
                steps={
                    r["name"]: {
                        "payload": json.loads(r["payload"]),
                        "result": json.loads(r["result"]) if r["result"] else None,
                    }
                    for r in conn.execute("SELECT * FROM steps WHERE run_id=?", (reference,))
                },
            )

    def claim(self, reference, scope, owner):
        self.read(reference, scope)
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE runs SET owner=? WHERE id=? AND owner IS NULL", (owner, reference)
            ).rowcount
            if changed != 1:
                raise AccessDenied("Another or unresolved controller owns this workflow")

    def release(self, reference, owner):
        with self.transaction() as conn:
            conn.execute("UPDATE runs SET owner=NULL WHERE id=? AND owner=?", (reference, owner))

    def intend(self, reference, owner, name, payload):
        with self.transaction() as conn:
            if (
                conn.execute("SELECT owner FROM runs WHERE id=?", (reference,)).fetchone()[0]
                != owner
            ):
                raise AccessDenied("Workflow claim changed")
            old = conn.execute(
                "SELECT payload,result FROM steps WHERE run_id=? AND name=?", (reference, name)
            ).fetchone()
            if old:
                return {
                    "payload": json.loads(old[0]),
                    "result": json.loads(old[1]) if old[1] else None,
                }
            conn.execute(
                "INSERT INTO steps VALUES(?,?,?,NULL)", (reference, name, canonical(payload))
            )
            return None

    def finish(self, reference, owner, name, result, *, attempt_id=None):
        with self.transaction() as conn:
            row = conn.execute("SELECT owner,body FROM runs WHERE id=?", (reference,)).fetchone()
            if row["owner"] != owner:
                raise AccessDenied("Workflow claim changed")
            changed = conn.execute(
                "UPDATE steps SET result=? WHERE run_id=? AND name=? AND result IS NULL",
                (canonical(result), reference, name),
            ).rowcount
            if changed != 1:
                raise AccessDenied("Workflow result already exists or intent is absent")
            if attempt_id:
                body = json.loads(row["body"])
                body["attempt_id"] = attempt_id
                conn.execute("UPDATE runs SET body=? WHERE id=?", (canonical(body), reference))

    def lineage(self, task_id, attempt_id, fact):
        key = fingerprint({"common": fact["common"], "branch": fact["branch"]})
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM lineage WHERE resource=?", (key,)).fetchone()
            if old and (old["task_id"], old["attempt_id"], old["body"]) != (
                task_id,
                attempt_id,
                canonical(fact),
            ):
                raise AccessDenied("Branch lineage belongs to different work or changed resources")
            conn.execute(
                "INSERT OR IGNORE INTO lineage VALUES(?,?,?,?)",
                (key, task_id, attempt_id, canonical(fact)),
            )

    def verified(self, scope, task_id, artifact_digest):
        """Read actual saved inspection only; this method never grants acceptance."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM runs WHERE scope=? AND task_id=?",
                (canonical(asdict(scope)), task_id),
            ).fetchone()
        if row is None:
            return None
        run = self.read(row["id"], scope)
        result = run["steps"].get("verify", {}).get("result")
        if result is None:
            return None
        body = {k: v for k, v in result.items() if k != "digest"}
        if (
            result.get("task_id") != task_id
            or result.get("attempt_id") != run["attempt_id"]
            or result.get("prose_digest") != run["prose_digest"]
            or result.get("digest") != fingerprint(body)
        ):
            raise SchemaError("Persisted inspection context or digest changed")
        return result if result["digest"] == artifact_digest else None
