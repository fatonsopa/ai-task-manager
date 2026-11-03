"""Explicit authored declarations; this module is never product authority."""

from dataclasses import replace
from uuid import uuid4

from ai_task_manager.domains.scheduling.models import DependencySnapshot
from ai_task_manager.domains.work.errors import EvidenceRequired


class SnapshotSource:
    def __init__(self):
        self.receipts = {}

    def issue(self, snapshot):
        reference = "authored-dependency:" + str(uuid4())
        self.receipts[reference] = snapshot
        return reference

    def verify_snapshot(self, reference):
        if reference not in self.receipts:
            raise EvidenceRequired("Authored dependency receipt is unavailable")
        return self.receipts[reference]


def snapshot(core, handle, task, now, dependencies=(), priority="normal", **changes):
    current = core.scheduling.explain(handle, task.id)
    _, scope = core.session_binding(handle)
    value = DependencySnapshot(
        scope, task.id, task.prose_digest, task.issue_id, "authored-fixture-source",
        (current["source_revision"] or 0) + 1, now, now + 30,
        "complete", priority, tuple(dependencies), current["graph_version"],
    )
    return replace(value, **changes)


def declare(core, handle, task, source, now, dependencies=(), priority="normal", **changes):
    value = snapshot(core, handle, task, now, dependencies, priority, **changes)
    reference = source.issue(value)
    key = "authored-dependency-operation:" + str(uuid4())
    return core.scheduling.observe(
        handle, task.id, task.version, task.decision_revision, reference, key
    )


def known_declaration(rig, task, handle=None):
    """Only tasks explicitly created by this fixture have a known empty graph."""
    declarations = getattr(rig, "known_dependencies", {})
    if task.id not in declarations:
        return
    dependencies, priority = declarations[task.id]
    return declare(rig.core, handle or rig.handle, task, rig.scheduling_sources,
                   rig.now, dependencies, priority)


LEGACY_DATABASE_SHA256 = '69f371033dac489a37e64252e99c2c124fe89713af13aa314c50566016834920'
LEGACY_DATABASE_SOURCE = r'''import json
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

    def __init__(self, path: str | Path, *, guard=None):
        self.path = str(path)
        self.guard = guard
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
                if len(rows) != 1 or rows[0][0] not in {1, 2, 3}:
                    raise SchemaError("Unsupported task store schema")
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
            if self.guard:
                self.guard(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

        # The schema version covers the additive tables as well as earlier records.
        with self.transaction() as conn:
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
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self.guard:
                self.guard(conn)
            yield conn
            if self.guard:
                self.guard(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
'''

LEGACY_LEASE_SOURCE = 'def _work_lease(self, conn, handle, attempt_id, expected_attempt_version):\n        sid, scope = sessions.resolve(conn, handle)\n        attempt = attempts.get(conn, sid, attempt_id)\n        task = work.get(conn, sid, attempt.task_id)\n        if attempt.version != expected_attempt_version:\n            raise StaleVersion("Attempt version has changed")\n        if (\n            attempt.state != A.RUNNING\n            or task.human_hold\n            or task.delivered_once\n            or task.decision_revision != attempt.decision_revision\n            or task.observed_status != S.IN_PROGRESS\n            or task.sync_state != SyncState.CONFIRMED\n        ):\n            raise InvalidTransition("Work lease is no longer authorized")\n        self.require_open(conn, scope, task)\n        return attempt\n\ndef validate_work_lease(self, handle, attempt_id, expected_attempt_version):\n        """Recheck authority at a host work boundary, including later human decisions."""\n        with self.c.db.transaction() as conn:\n            return self._work_lease(conn, handle, attempt_id, expected_attempt_version)\n\ndef work_boundary(self, handle, attempt_id, expected_attempt_version):\n        """Serialize a bounded short effect against other task-authority writers.\n\n        The trusted adapter owns effect intent/reconciliation. This transaction cannot\n        roll back a filesystem publication or process spawn, and must never surround\n        a whole command, host call, provider request or model turn.\n        """\n        with self.c.db.transaction() as conn:\n            yield self._work_lease(conn, handle, attempt_id, expected_attempt_version)'


def legacy_database_type(version=3):
    import hashlib
    from pathlib import Path

    if version == 3:
        source, label = LEGACY_DATABASE_SOURCE, "frozen_schema3_database"
        assert hashlib.sha256(source.encode()).hexdigest() == LEGACY_DATABASE_SHA256
    else:
        expected = {
            1: "6770c2d43cc552d2b1254a8f0938a7bf35cc87abefebd5b192ce3f48e3e04582",
            2: "948c9c1b4b8a88eecd906c5094576f2edee3957960d3d29dc8d6ea5c26cbb0cd",
        }
        path = Path(__file__).with_name("fixtures") / f"schema{version}_database.py"
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected[version]
        source, label = data.decode(), str(path)
    namespace = {"__name__": "frozen_historical_database"}
    exec(compile(source, label, "exec"), namespace)
    return namespace["Database"]


# Exact positive canonical-fence dependency from own-product 95d758f18abfe3682f1382c09e06770fad97b9ab.
LEGACY_CANONICAL_SOURCE = 'def identity(scope, issue_id):\n    raw = json.dumps(\n        {"provider": scope.provider, "workspace": scope.workspace, "issue_id": issue_id},\n        sort_keys=True,\n        separators=(",", ":"),\n        ensure_ascii=False,\n    )\n    return hashlib.sha256(raw.encode()).hexdigest()\n\ndef _facts(conn, scope, issue_id):\n    row = conn.execute(\n        "SELECT delivered,terminal FROM issue_lifecycle WHERE identity_key=?",\n        (identity(scope, issue_id),),\n    ).fetchone()\n    if row and any(type(value) is not int or value not in {0, 1} for value in row):\n        raise SchemaError("Canonical lifecycle facts are invalid")\n    return row\n\ndef require_open(conn, scope, issue_id):\n    row = _facts(conn, scope, issue_id)\n    if row and (row[0] or row[1]):\n        raise EvidenceRequired("Delivered or terminal history requires a new authorized issue")'
LEGACY_EXECUTION_OPEN_SOURCE = 'def require_open(conn, scope, task):\n        try:\n            issue_claims.require_open(conn, scope, task.issue_id)\n        except EvidenceRequired as exc:\n            raise InvalidTransition(\n                "Delivered or terminal work requires a new linked issue"\n            ) from exc'
LEGACY_CANONICAL_SHA256 = '08789889f3c9f91d5b3a9a33cc685c70246f58f9774a5dfbb957b10cf2cda2d7'
LEGACY_EXECUTION_OPEN_SHA256 = 'e9e613d7e30f0e16c8edec3df05e7d0a527ae0ee1a38ee5a4f8545f145cc54e1'


def legacy_execution(context):
    import hashlib
    import json
    from types import SimpleNamespace
    from contextlib import contextmanager
    from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition, SchemaError, StaleVersion
    from ai_task_manager.domains.work.models import AttemptState, IssueStatus, SyncState
    from ai_task_manager.storage import attempts, sessions, work
    assert hashlib.sha256(LEGACY_CANONICAL_SOURCE.encode()).hexdigest() == LEGACY_CANONICAL_SHA256
    assert hashlib.sha256(LEGACY_EXECUTION_OPEN_SOURCE.encode()).hexdigest() == LEGACY_EXECUTION_OPEN_SHA256
    canonical = dict(hashlib=hashlib, json=json, EvidenceRequired=EvidenceRequired, SchemaError=SchemaError)
    exec(compile(LEGACY_CANONICAL_SOURCE, "frozen_schema3_canonical", "exec"), canonical)
    namespace = dict(A=AttemptState, S=IssueStatus, SyncState=SyncState,
                     InvalidTransition=InvalidTransition, StaleVersion=StaleVersion,
                     EvidenceRequired=EvidenceRequired,
                     issue_claims=SimpleNamespace(require_open=canonical["require_open"]),
                     attempts=attempts, sessions=sessions, work=work)
    exec(compile(LEGACY_EXECUTION_OPEN_SOURCE, "frozen_schema3_open", "exec"), namespace)
    exec(compile(LEGACY_LEASE_SOURCE, "frozen_schema3_lease", "exec"), namespace)
    fixture = type("CachedSchema3Execution", (), {
        "_work_lease": namespace["_work_lease"],
        "validate_work_lease": namespace["validate_work_lease"],
        "require_open": staticmethod(namespace["require_open"]),
        "work_boundary": contextmanager(namespace["work_boundary"]),
    })()
    fixture.c = context
    return fixture


def seed_known_queue(rig, task):
    """Explicit old-test precondition; not a product observation or alternate writer."""
    from dataclasses import asdict
    from ai_task_manager.domains.scheduling import policy
    from ai_task_manager.storage import scheduling, sessions, work

    if task.id not in getattr(rig, "known_dependencies", {}):
        return
    dependencies, priority = rig.known_dependencies[task.id]
    with rig.db.transaction() as conn:
        sid, scope = sessions.resolve(conn, rig.handle)
        current = work.get(conn, sid, task.id)
        if scheduling.current(conn, current.id) is not None:
            return
        now = rig.core.context.clock()
        value = DependencySnapshot(scope, current.id, current.prose_digest,
            current.issue_id, "authored-fixture-source", 1, now, now + 30,
            "complete", priority, tuple(dependencies), 0)
        policy.validate(value, now)
        reference = rig.scheduling_sources.issue(value)
        assert rig.scheduling_sources.verify_snapshot(reference) == value
        policy.check_cycles({current.id: tuple(edge.predecessor for edge in dependencies)})
        scheduling.save(conn, value, 1, work.fingerprint(asdict(value)))


class LegacyMigrationFixture:
    """Declared stored history on genuine public old schemas; no current Core calls."""

    def __init__(self, path, version):
        from ai_task_manager.domains.work.models import Scope
        from ai_task_manager.storage import sessions

        self.db = legacy_database_type(version)(path)
        self.scope = Scope("linear", "workspace", "principal", "project", "legacy-session")
        with self.db.transaction() as conn:
            self.handle = sessions.register(conn, self.scope)
            self.sid, _ = sessions.resolve(conn, self.handle)
            assert conn.execute("SELECT version FROM metadata").fetchone()[0] == version
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name IN "
                                    "('scheduling_snapshots','store_identity')").fetchone()

    def task(self, *, confirmed=True):
        from ai_task_manager.domains.work.models import IssueStatus, SyncState, Task
        from ai_task_manager.storage import events, outbox, work

        identifier = str(uuid4())
        task = Task(identifier, self.sid, "Retain historical work", "Retain exact stored history.",
            "a" * 64, "exact-v1", "authored-historical-approval", True, ("complete",),
            issue_id="legacy-issue:" + identifier if confirmed else None,
            observed_status=IssueStatus.BACKLOG if confirmed else None,
            desired_status=IssueStatus.BACKLOG,
            sync_state=SyncState.CONFIRMED if confirmed else SyncState.PENDING,
            execution_state="idle" if confirmed else "pending_intake")
        with self.db.transaction() as conn:
            work.insert(conn, task)
            event = events.append(conn, self.sid, task.id, "historical_intake", {
                "task_version": task.version, "decision_revision": 0})
            operation = outbox.append(conn, self.sid, task.id, "create_backlog", {
                "task_version": task.version, "desired_status": "Backlog",
                "issue_id": task.issue_id, "prose_digest": task.prose_digest,
                "event_id": event.id})
            if confirmed:
                conn.execute("UPDATE outbox SET state='confirmed' WHERE id=?", (operation,))
        return task

    def current(self, task):
        from ai_task_manager.storage import work
        with self.db.transaction() as conn:
            return work.get(conn, self.sid, task.id)

    def terminal_observation(self, task, payload):
        from ai_task_manager.storage.database import encode
        from ai_task_manager.storage.work import fingerprint
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO external_observations VALUES(?,?,?,?,?,?,?)", (
                str(uuid4()), task.id, payload["source_revision"], fingerprint(payload),
                "authored-historical-observation", encode(payload), "pending"))
