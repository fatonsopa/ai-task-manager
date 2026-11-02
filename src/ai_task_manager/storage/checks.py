"""Independent durable check plans/results; no task-state or provider tables."""

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from ai_task_manager.config.files import private_path, strict_json
from ai_task_manager.domains.checks.models import Plan, canonical, identifier
from ai_task_manager.domains.checks.results import aggregate, judge
from ai_task_manager.domains.checks.reuse import eligible
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    IdempotencyConflict,
    InvalidTransition,
    NotFound,
    SchemaError,
    StaleVersion,
    ValidationError,
    WorkError,
)

_TABLES = {"check_meta", "check_plans", "check_groups", "check_events", "check_proofs"}
_SCHEMA = (
    "CREATE TABLE check_meta(version INTEGER NOT NULL, store_id TEXT NOT NULL)",
    "CREATE TABLE check_plans(ref TEXT PRIMARY KEY, scope TEXT NOT NULL, "
    "operation_key TEXT NOT NULL, "
    "digest TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(scope,operation_key))",
    "CREATE TABLE check_groups(plan_ref TEXT NOT NULL, name TEXT NOT NULL, "
    "version INTEGER NOT NULL, "
    "state TEXT NOT NULL, intent TEXT, result TEXT, PRIMARY KEY(plan_ref,name))",
    "CREATE TABLE check_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, plan_ref TEXT NOT NULL, "
    "kind TEXT NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE check_proofs(ref TEXT PRIMARY KEY, scope TEXT NOT NULL, unit TEXT NOT NULL, "
    "body TEXT NOT NULL)",
)


class CheckStoreBusy(WorkError):
    """Retryable infrastructure contention; it does not revoke store ownership."""

    retryable = True
    code = "check_store_busy"


def _busy(error):
    return getattr(error, "sqlite_errorcode", 0) & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


class CheckStore:
    def __init__(self, path, *, faults=None):
        self.path, self.faults = Path(path).absolute(), faults
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if self.path.parent.is_symlink() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise AccessDenied("Check store requires a private owned directory")
        private_path(self.path, existing=False)
        try:
            with self.path.open("xb"):
                self.path.chmod(0o600)
        except FileExistsError:
            pass
        self.file_identity = self._identity()
        with self._transaction() as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            tables.discard("sqlite_sequence")
            if tables and tables != _TABLES:
                raise SchemaError("Not an enrolled checks store")
            if not tables:
                for sql in _SCHEMA:
                    conn.execute(sql)
                conn.execute("INSERT INTO check_meta VALUES(2,?)", (str(uuid4()),))
            rows = conn.execute("SELECT version,store_id FROM check_meta").fetchall()
            if len(rows) != 1 or rows[0][0] != 2:
                raise SchemaError("Unsupported checks store format")
            try:
                if str(UUID(rows[0][1])) != rows[0][1]:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise SchemaError("Invalid checks store identity") from None
            self.store_id = rows[0][1]

    def _identity(self):
        s = private_path(self.path).stat()
        return s.st_dev, s.st_ino

    def _binding(self, conn):
        if self._identity() != self.file_identity:
            raise AccessDenied("Checks store file identity changed")
        main = [r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"]
        if len(main) != 1:
            raise AccessDenied("Checks connection has no unique main database")
        info = private_path(main[0]).stat()
        if (info.st_dev, info.st_ino) != self.file_identity:
            raise AccessDenied("Checks connection path binding changed")
        if hasattr(self, "store_id"):
            rows = conn.execute("SELECT version,store_id FROM check_meta").fetchall()
            if len(rows) != 1 or tuple(rows[0]) != (2, self.store_id):
                raise AccessDenied("Checks store binding changed")

    def _validate_connection(self, conn):
        """Exercise SQLite's moved-file protection, following the Core guard semantics.

        This requires DELETE journaling and a brief writer lock, even for reads.
        It does not attest a descriptor or support concurrent live replacement.
        """
        self._binding(conn)
        if [r[0] for r in conn.execute("PRAGMA main.journal_mode")] != ["delete"]:
            raise AccessDenied("Checks connection validation requires DELETE journal mode")
        if not hasattr(self, "store_id"):
            return  # Initial schema creation is validated before its transaction commits.
        temporary = str(uuid4())
        if str(UUID(temporary)) != temporary or temporary == self.store_id:
            raise AccessDenied("Checks validation identity is unavailable")
        try:
            conn.execute("SAVEPOINT check_store_validation")
            changed = conn.execute(
                "UPDATE main.check_meta SET store_id=? WHERE version=2 AND store_id=?",
                (temporary, self.store_id),
            ).rowcount
            if changed != 1:
                raise AccessDenied("Checks validation did not uniquely match the store")
            conn.execute("ROLLBACK TO check_store_validation")
            conn.execute("RELEASE check_store_validation")
        except sqlite3.Error as error:
            if _busy(error):
                raise CheckStoreBusy(
                    "Check store is busy; retry after the writer releases"
                ) from error
            raise AccessDenied("Checks connection coherence could not be validated") from error
        self._binding(conn)

    @contextmanager
    def _transaction(self, *, write=True):
        if self._identity() != self.file_identity:
            raise AccessDenied("Checks store file identity changed")
        conn = sqlite3.connect(self.path, timeout=5 if write else 0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._validate_connection(conn)
            yield conn
            self._validate_connection(conn)
            conn.commit()
        except sqlite3.Error as error:
            conn.rollback()
            if _busy(error):
                raise CheckStoreBusy(
                    "Check store is busy; retry after the writer releases"
                ) from error
            raise
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _fault(self, stage):
        if self.faults is not None:
            self.faults.hit(stage)

    @staticmethod
    def _plan(conn, ref, scope):
        row = conn.execute("SELECT * FROM check_plans WHERE ref=?", (ref,)).fetchone()
        if row is None or row["scope"] != canonical(asdict(scope)):
            raise NotFound("Scoped check plan unavailable")
        try:
            plan = Plan.restore(strict_json(row["body"]))
            if plan.digest != row["digest"]:
                raise SchemaError("Stored check plan digest mismatch")
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise SchemaError("Stored check plan is malformed") from error
        return plan

    @staticmethod
    def _event(conn, ref, kind, body):
        conn.execute(
            "INSERT INTO check_events(plan_ref,kind,body) VALUES(?,?,?)",
            (ref, kind, canonical(body)),
        )

    def create(self, plan, key):
        identifier(key)
        raw, scope = canonical(plan.record()), canonical(asdict(plan.scope))
        if len(raw.encode()) > 262144:
            raise ValidationError("Check plan exceeds the durable record limit")
        with self._transaction() as conn:
            old = conn.execute(
                "SELECT ref,digest FROM check_plans WHERE scope=? AND operation_key=?", (scope, key)
            ).fetchone()
            if old is not None:
                if old["digest"] != plan.digest:
                    raise IdempotencyConflict("Check key has different immutable inputs")
                return old["ref"]
            ref = "check-plan:" + str(uuid4())
            conn.execute(
                "INSERT INTO check_plans VALUES(?,?,?,?,?)", (ref, scope, key, plan.digest, raw)
            )
            for group in plan.groups:
                conn.execute(
                    "INSERT INTO check_groups VALUES(?,?,0,'pending',NULL,NULL)", (ref, group.key)
                )
            self._event(conn, ref, "plan_created", {"digest": plan.digest})
            self._fault("plan_before_commit")
            return ref

    def read(self, ref, scope):
        with self._transaction(write=False) as conn:
            plan = self._plan(conn, ref, scope)
            groups = {}
            for row in conn.execute("SELECT * FROM check_groups WHERE plan_ref=?", (ref,)):
                groups[row["name"]] = {
                    "version": row["version"],
                    "state": row["state"],
                    "intent": strict_json(row["intent"]) if row["intent"] else None,
                    "result": strict_json(row["result"]) if row["result"] else None,
                }
            events = [
                {"sequence": r["sequence"], "kind": r["kind"], "body": strict_json(r["body"])}
                for r in conn.execute(
                    "SELECT * FROM check_events WHERE plan_ref=? ORDER BY sequence", (ref,)
                )
            ]
            results = [g["result"] for g in groups.values() if g["result"] is not None]
            outcome = (
                aggregate(results)
                if all(g["state"] == "complete" for g in groups.values())
                else "blocked"
            )
            return {"plan": plan, "groups": groups, "events": events, "outcome": outcome}

    def proofs(self, plan, unit):
        with self._transaction(write=False) as conn:
            proofs = [
                strict_json(r[0])
                for r in conn.execute(
                    "SELECT body FROM check_proofs WHERE scope=? AND unit=? "
                    "ORDER BY rowid DESC LIMIT 100",
                    (canonical(asdict(plan.scope)), unit.name),
                )
            ]
            for proof in proofs:
                source = self._plan(conn, proof["plan_ref"], plan.scope)
                row = conn.execute(
                    "SELECT result FROM check_groups WHERE plan_ref=? AND name=? "
                    "AND state='complete'",
                    (proof["plan_ref"], proof["group"]),
                ).fetchone()
                result = strict_json(row[0]) if row and row[0] else {}
                original = next((u for u in source.units if u.name == proof["unit"]), None)
                if (
                    original is None
                    or not eligible(source, original, proof)
                    or result.get("ref") != proof["physical_origin"]
                    or proof["unit"] not in result.get("passed", ())
                    or proof["unit"] not in result.get("ran", ())
                ):
                    raise SchemaError("Stored proof lacks its exact physical result")
            return proofs

    def begin(self, ref, scope, group, expected_version, dispatched, carried):
        with self._transaction() as conn:
            plan = self._plan(conn, ref, scope)
            row = conn.execute(
                "SELECT * FROM check_groups WHERE plan_ref=? AND name=?", (ref, group)
            ).fetchone()
            if row is None:
                raise NotFound("Planned group unavailable")
            if row["version"] != expected_version:
                raise StaleVersion("Check group version changed")
            if row["state"] != "pending":
                raise InvalidTransition("Existing dispatch must be reconciled, not repeated")
            units = {u.name: u for u in plan.units if u.group == group}
            if (
                len(dispatched) != len(set(dispatched))
                or set(dispatched).intersection(carried)
                or set(dispatched) | set(carried) != set(units)
            ):
                raise ValidationError("Dispatch and carry must exactly cover selected units")
            for name, proof in carried.items():
                stored = conn.execute(
                    "SELECT body FROM check_proofs WHERE ref=?", (proof.get("ref"),)
                ).fetchone()
                if (
                    stored is None
                    or strict_json(stored[0]) != proof
                    or not eligible(plan, units[name], proof)
                ):
                    raise AccessDenied("Carry proof does not belong to this scoped store")
            intent = {"dispatched": list(dispatched), "carried": carried}
            conn.execute(
                "UPDATE check_groups SET state='intent',version=version+1,intent=? "
                "WHERE plan_ref=? AND name=?",
                (canonical(intent), ref, group),
            )
            self._event(conn, ref, "dispatch_intent", {"group": group, **intent})
            self._fault("dispatch_before_commit")
            return expected_version + 1

    def finish(
        self, ref, scope, group, expected_version, execution=None, *, baseline=None, error=None
    ):
        with self._transaction() as conn:
            plan = self._plan(conn, ref, scope)
            row = conn.execute(
                "SELECT * FROM check_groups WHERE plan_ref=? AND name=?", (ref, group)
            ).fetchone()
            if row is None or row["version"] != expected_version:
                raise StaleVersion("Check group version changed")
            if row["state"] != "intent":
                raise InvalidTransition("Result requires a durable dispatch intent")
            intent = strict_json(row["intent"])
            dispatched, carried = intent["dispatched"], intent["carried"]
            if error is not None:
                identifier(error)
                if execution is not None:
                    result = judge(plan, group, dispatched, execution)
                    result.update(outcome="unknown", passed=[], error_type=error)
                else:
                    result = {"outcome": "unknown", "ran": [], "passed": [], "error_type": error}
            elif dispatched:
                result = judge(plan, group, dispatched, execution, baseline=baseline)
            elif execution is not None:
                raise ValidationError("An empty dispatch cannot claim execution")
            else:
                result = {"outcome": "passed" if carried else "skipped", "ran": [], "passed": []}
            result_ref = "check-result:" + str(uuid4())
            result.update(
                ref=result_ref,
                selected=sorted(dispatched + list(carried)),
                dispatched=dispatched,
                carried={n: v["ref"] for n, v in carried.items()},
                not_run=sorted(set(dispatched) - set(result["ran"])),
                physical_count=(None if error and execution is None else len(result["ran"])),
                carried_count=len(carried),
            )
            units = {u.name: u for u in plan.units if u.group == group}
            for name in result["passed"]:
                unit = units[name]
                if not plan.cacheable or not unit.reusable:
                    continue
                proof = {
                    "ref": "check-proof:" + str(uuid4()),
                    "scope": asdict(plan.scope),
                    "plan_ref": ref,
                    "unit": name,
                    "group": group,
                    "head": plan.head,
                    "input_digest": unit.input_digest,
                    "catalog_digest": plan.catalog_digest,
                    "binding_digest": plan.binding_digest,
                    "producer_digest": plan.producer_digest,
                    "source_digest": plan.source_digest,
                    "physical_origin": result_ref,
                    "result_ref": result_ref,
                    "passed": True,
                }
                conn.execute(
                    "INSERT INTO check_proofs VALUES(?,?,?,?)",
                    (proof["ref"], canonical(asdict(scope)), name, canonical(proof)),
                )
            conn.execute(
                "UPDATE check_groups SET state='complete',version=version+1,result=? "
                "WHERE plan_ref=? AND name=?",
                (canonical(result), ref, group),
            )
            self._event(conn, ref, "group_result", {"group": group, "result": result})
            self._fault("result_before_commit")
            return result

    def block(self, ref, scope, group, expected_version, reason):
        identifier(reason)
        with self._transaction() as conn:
            self._plan(conn, ref, scope)
            row = conn.execute(
                "SELECT * FROM check_groups WHERE plan_ref=? AND name=?", (ref, group)
            ).fetchone()
            if row is None or row["version"] != expected_version:
                raise StaleVersion("Check group version changed")
            if row["state"] != "pending":
                raise InvalidTransition("Only an undispatched dependent group may be blocked")
            result = {
                "outcome": "unknown",
                "reason": reason,
                "ran": [],
                "passed": [],
                "physical_count": 0,
                "carried_count": 0,
            }
            conn.execute(
                "UPDATE check_groups SET state='complete',version=version+1,result=? "
                "WHERE plan_ref=? AND name=?",
                (canonical(result), ref, group),
            )
            self._event(conn, ref, "dependent_blocked", {"group": group, "reason": reason})
