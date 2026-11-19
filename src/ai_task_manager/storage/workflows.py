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
from ai_task_manager.storage.workflow_migration import (
    CURRENT_TABLES,
    LEGACY_TABLES,
    MARKER,
    preserve_schema1,
    rollback_schema1,
    seal_migration,
)


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
                conn.execute("INSERT INTO workflow_meta VALUES(2,?)", (str(uuid4()),))
                conn.execute(
                    "CREATE TABLE runs(id TEXT PRIMARY KEY,scope TEXT,task_id TEXT,"
                    "operation_key TEXT,"
                    "body TEXT,owner TEXT,UNIQUE(scope,operation_key))"
                )
                conn.execute(
                    "CREATE TABLE steps(run_id TEXT,name TEXT,payload TEXT,result TEXT,"
                    "PRIMARY KEY(run_id,name))"
                )
                conn.execute(
                    "CREATE TABLE lineage(resource TEXT PRIMARY KEY,task_id TEXT,"
                    "attempt_id TEXT,body TEXT)"
                )
                conn.execute(
                    "CREATE TABLE workflow_heads(scope TEXT,task_id TEXT,run_id TEXT UNIQUE,"
                    "PRIMARY KEY(scope,task_id))"
                )
                conn.execute(
                    "CREATE TABLE recoveries(predecessor TEXT PRIMARY KEY,successor TEXT UNIQUE,"
                    "scope TEXT,operation_key TEXT,request TEXT,UNIQUE(scope,operation_key))"
                )
            elif tables not in (
                LEGACY_TABLES,
                CURRENT_TABLES,
                CURRENT_TABLES | {MARKER},
            ):
                raise SchemaError("Not an owned workflow store")
            rows = conn.execute("SELECT version,store_id FROM workflow_meta").fetchall()
            if len(rows) != 1 or rows[0][0] not in {1, 2}:
                raise SchemaError("Unsupported workflow store")
            if (
                tables
                and tables
                not in {1: (LEGACY_TABLES,), 2: (CURRENT_TABLES, CURRENT_TABLES | {MARKER})}[
                    rows[0][0]
                ]
            ):
                raise SchemaError("Workflow schema does not match its recorded version")
            if rows[0][0] == 1:
                origin = preserve_schema1(conn, self.path, self.identity, rows[0][1])
                # Copy every byte of the old records; only the per-task uniqueness
                # moves to a current-run pointer. SQLite rolls back partial migration.
                conn.execute(
                    "CREATE TABLE runs_v2(id TEXT PRIMARY KEY,scope TEXT,task_id TEXT,"
                    "operation_key TEXT,body TEXT,owner TEXT,UNIQUE(scope,operation_key))"
                )
                conn.execute("INSERT INTO runs_v2 SELECT * FROM runs")
                conn.execute("DROP TABLE runs")
                conn.execute("ALTER TABLE runs_v2 RENAME TO runs")
                conn.execute(
                    "CREATE TABLE workflow_heads(scope TEXT,task_id TEXT,run_id TEXT UNIQUE,"
                    "PRIMARY KEY(scope,task_id))"
                )
                conn.execute("INSERT INTO workflow_heads SELECT scope,task_id,id FROM runs")
                conn.execute(
                    "CREATE TABLE recoveries(predecessor TEXT PRIMARY KEY,successor TEXT UNIQUE,"
                    "scope TEXT,operation_key TEXT,request TEXT,UNIQUE(scope,operation_key))"
                )
                conn.execute("UPDATE workflow_meta SET version=2")
                seal_migration(conn, origin)
            self.store_id = rows[0][1]

    @classmethod
    def open_existing(cls, path, *, store_id):
        """Open retained schema-2 history without creation, migration or UUID adoption."""
        value = cls.__new__(cls)
        value.path = private_path(path)
        info = value.path.stat()
        value.identity = info.st_dev, info.st_ino
        value.store_id = store_id
        value._existing_only = True
        with value.transaction() as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables not in (CURRENT_TABLES, CURRENT_TABLES | {MARKER}):
                raise SchemaError("Not an owned workflow store")
        return value

    rollback_schema1 = staticmethod(rollback_schema1)

    @contextmanager
    def transaction(self):
        info = private_path(self.path).stat()
        if (info.st_dev, info.st_ino) != self.identity:
            raise AccessDenied("Workflow file identity changed")
        conn = sqlite3.connect(
            self.path.as_uri() + "?mode=rw"
            if getattr(self, "_existing_only", False)
            else self.path,
            uri=getattr(self, "_existing_only", False),
            isolation_level=None,
            timeout=2,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            if hasattr(self, "store_id"):
                probe_connection(
                    conn, StoreBinding(self.path, *self.identity, "workflow_meta", 2, self.store_id)
                )
            yield conn
            if hasattr(self, "store_id"):
                probe_connection(
                    conn, StoreBinding(self.path, *self.identity, "workflow_meta", 2, self.store_id)
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _body(task, plan, runtime_digest, history):
        return {
            "task_id": task.id,
            "prose_digest": task.prose_digest,
            "decision_revision": task.decision_revision,
            "plan_id": plan.id,
            "plan_digest": plan.digest,
            "runtime_digest": runtime_digest,
            "attempt_id": None,
            "task_version": task.version,
            "plan": asdict(plan),
            "recovery_history": history,
        }

    def create(self, scope, task, plan, key, runtime_digest=None, history=None):
        body = self._body(task, plan, runtime_digest, history)
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT * FROM runs WHERE scope=? AND operation_key=?",
                (canonical(asdict(scope)), key),
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
            if conn.execute(
                "SELECT 1 FROM workflow_heads WHERE scope=? AND task_id=?",
                (canonical(asdict(scope)), task.id),
            ).fetchone():
                raise IdempotencyConflict("Task already has a workflow; trusted recovery required")
            ref = "workflow:" + str(uuid4())
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,NULL)",
                (ref, canonical(asdict(scope)), task.id, key, canonical(body)),
            )
            conn.execute(
                "INSERT INTO workflow_heads VALUES(?,?,?)",
                (canonical(asdict(scope)), task.id, ref),
            )
            return ref

    def find(self, scope, task_id, operation_key):
        """Exact old keys retain old history even after a successor is admitted."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id,task_id FROM runs WHERE scope=? AND operation_key=?",
                (canonical(asdict(scope)), operation_key),
            ).fetchone()
            if row:
                if row["task_id"] != task_id:
                    raise IdempotencyConflict("Workflow key belongs to another task")
                return row["id"]
            if conn.execute(
                "SELECT 1 FROM workflow_heads WHERE scope=? AND task_id=?",
                (canonical(asdict(scope)), task_id),
            ).fetchone():
                raise IdempotencyConflict("Workflow task or original operation key changed")
            return None

    def recovery_result(self, scope, predecessor, request):
        with self.transaction() as conn:
            return self._recovery_result(conn, scope, predecessor, request)

    @staticmethod
    def _recovery_result(conn, scope, predecessor, request):
        row = conn.execute(
            "SELECT * FROM recoveries WHERE predecessor=?", (predecessor,)
        ).fetchone()
        if row is None:
            return None
        if row["scope"] != canonical(asdict(scope)) or row["request"] != canonical(request):
            raise IdempotencyConflict("Expired workflow already has a different recovery")
        return row["successor"]

    def recover(
        self,
        scope,
        predecessor,
        task,
        plan,
        key,
        runtime_digest,
        history,
        request,
        check,
        *,
        settled=False,
        retain_completed=False,
    ):
        """Publish a successor only inside the application's guarded boundary."""
        with self.transaction() as conn:
            prior = self._recovery_result(conn, scope, predecessor, request)
            if prior is not None:
                return prior
            old = conn.execute(
                "SELECT * FROM runs WHERE id=? AND scope=? AND task_id=?",
                (predecessor, canonical(asdict(scope)), task.id),
            ).fetchone()
            if old is None or (old["owner"] is not None and not settled):
                raise AccessDenied("Original unclaimed scoped workflow is required")
            head = conn.execute(
                "SELECT run_id FROM workflow_heads WHERE scope=? AND task_id=?",
                (canonical(asdict(scope)), task.id),
            ).fetchone()
            if head is None or head[0] != predecessor:
                raise IdempotencyConflict("Only the current expired workflow can be recovered")
            if conn.execute(
                "SELECT 1 FROM runs WHERE scope=? AND operation_key=?",
                (canonical(asdict(scope)), key),
            ).fetchone():
                raise IdempotencyConflict("Recovery requires a new scoped operation key")
            if (
                not settled
                and conn.execute("SELECT 1 FROM lineage WHERE task_id=?", (task.id,)).fetchone()
            ):
                raise AccessDenied("Resource lineage prevents zero-effect recovery")
            steps = conn.execute("SELECT * FROM steps WHERE run_id=?", (predecessor,)).fetchall()
            if not settled and any(
                row["name"] != "start" or row["result"] is not None for row in steps
            ):
                raise AccessDenied("Only an unexecuted start intent can be recovered")
            check(json.loads(old["body"]))
            ref = "workflow:" + str(uuid4())
            body = self._body(task, plan, runtime_digest, history)
            if settled:
                body["succession"] = {
                    "version": 2,
                    "predecessor": predecessor,
                    "authorization": request,
                    "retained": {
                        "body": json.loads(old["body"]),
                        "steps": {
                            step["name"]: {
                                "payload": json.loads(step["payload"]),
                                "result": json.loads(step["result"]) if step["result"] else None,
                            }
                            for step in steps
                        },
                    },
                }
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,NULL)",
                (ref, canonical(asdict(scope)), task.id, key, canonical(body)),
            )
            if retain_completed:
                body["attempt_id"] = json.loads(old["body"])["attempt_id"]
                conn.execute("UPDATE runs SET body=? WHERE id=?", (canonical(body), ref))
                for step in steps:
                    if step["result"] is not None or step["name"] == "worker-commit":
                        conn.execute(
                            "INSERT INTO steps VALUES(?,?,?,?)",
                            (ref, step["name"], step["payload"], step["result"]),
                        )
            conn.execute(
                "INSERT INTO recoveries VALUES(?,?,?,?,?)",
                (predecessor, ref, canonical(asdict(scope)), key, canonical(request)),
            )
            changed = conn.execute(
                "UPDATE workflow_heads SET run_id=? WHERE scope=? AND task_id=? AND run_id=?",
                (ref, canonical(asdict(scope)), task.id, predecessor),
            ).rowcount
            if changed != 1:
                raise IdempotencyConflict("Workflow head changed during recovery")
            return ref

    def current(self, scope, task_id):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT run_id FROM workflow_heads WHERE scope=? AND task_id=?",
                (canonical(asdict(scope)), task_id),
            ).fetchone()
        return self.read(row["run_id"], scope) if row else None

    def contains(self, reference, scope):
        with self.transaction() as conn:
            return conn.execute("SELECT 1 FROM runs WHERE id=? AND scope=?",
                                (reference, canonical(asdict(scope)))).fetchone() is not None

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

    def original_key(self, reference, scope):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT operation_key FROM runs WHERE id=? AND scope=?",
                (reference, canonical(asdict(scope))),
            ).fetchone()
            if row is None:
                raise AccessDenied("Scoped workflow is unavailable")
            return row["operation_key"]

    def claim(self, reference, scope, owner, *, controller=None):
        self.read(reference, scope)
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM recoveries WHERE predecessor=?", (reference,)
            ).fetchone():
                raise AccessDenied("Expired predecessor is retained as history only")
            changed = conn.execute(
                "UPDATE runs SET owner=? WHERE id=? AND owner IS NULL", (owner, reference)
            ).rowcount
            if changed != 1:
                raise AccessDenied("Another or unresolved controller owns this workflow")
            if controller is not None:
                body = json.loads(
                    conn.execute("SELECT body FROM runs WHERE id=?", (reference,)).fetchone()[0]
                )
                body["claim_controller"] = controller
                conn.execute("UPDATE runs SET body=? WHERE id=?", (canonical(body), reference))

    def activate_successor(self, reference, scope, history):
        """Capture a new version-two owner's first forward witness exactly once."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT body FROM runs WHERE id=? AND scope=?",
                (reference, canonical(asdict(scope))),
            ).fetchone()
            if row is None:
                raise AccessDenied("Scoped successor is unavailable")
            body = json.loads(row[0])
            if body.get("succession", {}).get("version") != 2:
                raise AccessDenied("Legacy history cannot receive a new ownership witness")
            if body["recovery_history"] is not None:
                if body["recovery_history"] != history:
                    raise AccessDenied("Original successor owner history changed")
                return
            body["recovery_history"] = history
            conn.execute("UPDATE runs SET body=? WHERE id=?", (canonical(body), reference))

    def replace_successor_step(self, reference, owner, name, payload, result):
        """Record fresh linked authority while preserving the predecessor step."""
        with self.transaction() as conn:
            row = conn.execute("SELECT body,owner FROM runs WHERE id=?", (reference,)).fetchone()
            if row is None or row["owner"] != owner:
                raise AccessDenied("Current successor owner changed")
            body = json.loads(row["body"])
            original = body.get("succession", {}).get("retained", {}).get("steps", {}).get(name)
            current = conn.execute(
                "SELECT payload,result FROM steps WHERE run_id=? AND name=?", (reference, name)
            ).fetchone()
            if (
                name != "code-plan"
                or original is None
                or current is None
                or json.loads(current["payload"]) != original["payload"]
                or json.loads(current["result"]) != original["result"]
            ):
                raise AccessDenied("Only the exact inherited code authority may be linked once")
            conn.execute(
                "UPDATE steps SET payload=?,result=? WHERE run_id=? AND name=?",
                (canonical(payload), canonical(result), reference, name),
            )

    def release_dead_claim(self, reference, scope, owner, controller, check):
        """Release only an exact forward claim after its trusted owner check."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT owner,body FROM runs WHERE id=? AND scope=?",
                (reference, canonical(asdict(scope))),
            ).fetchone()
            if (
                row is None
                or row["owner"] != owner
                or json.loads(row["body"]).get("claim_controller") != controller
            ):
                raise AccessDenied("Original controller claim is missing or changed")
            check()
            conn.execute("UPDATE runs SET owner=NULL WHERE id=? AND owner=?", (reference, owner))

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

    def lineage(self, task_id, attempt_id, fact, *, reference=None, source_attempt=None):
        key = fingerprint({"common": fact["common"], "branch": fact["branch"]})
        resource_owner = source_attempt or attempt_id
        with self.transaction() as conn:
            if resource_owner != attempt_id:
                run = conn.execute("SELECT * FROM runs WHERE id=?", (reference,)).fetchone()
                if run is None or run["task_id"] != task_id:
                    raise AccessDenied("Current retained workflow is required")
                body = json.loads(run["body"])
                head = conn.execute(
                    "SELECT run_id FROM workflow_heads WHERE scope=? AND task_id=?",
                    (run["scope"], task_id),
                ).fetchone()
                if body["attempt_id"] != attempt_id or head is None or head[0] != reference:
                    raise AccessDenied("Retained branch use belongs to another workflow")
                require_resource_owner(body, resource_owner)
            old = conn.execute("SELECT * FROM lineage WHERE resource=?", (key,)).fetchone()
            if old and (old["task_id"], old["attempt_id"], old["body"]) != (
                task_id, resource_owner, canonical(fact)
            ):
                raise AccessDenied("Branch lineage belongs to different work or changed resources")
            conn.execute(
                "INSERT OR IGNORE INTO lineage VALUES(?,?,?,?)",
                (key, task_id, resource_owner, canonical(fact)),
            )

    def verified(self, scope, task_id, artifact_digest):
        """Read actual saved inspection only; this method never grants acceptance."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT run_id AS id FROM workflow_heads WHERE scope=? AND task_id=?",
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


def require_resource_owner(row, source_attempt):
    current = row
    while current.get("succession", {}).get("version") == 2:
        current = current["succession"]["retained"]["body"]
        if current.get("task_id") != row["task_id"]:
            break
        if current.get("attempt_id") == source_attempt:
            return
    raise AccessDenied("Resource owner is outside the recorded successor lineage")
