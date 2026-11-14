"""Private fixture intent records, not a product authority or evidence issuer."""

import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from ai_task_manager.config.files import digest, private_path


class DriverRefused(RuntimeError):
    """Fixed diagnostic code; callers must not append external exception text."""


def uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise DriverRefused("invalid_uuid") from error
    return value


@dataclass(frozen=True)
class Limits:
    deadline: float
    requests: int = 150
    projects: int = 1
    issues: int = 6
    webhooks: int = 1
    comments: int = 1
    started_at: float | None = None
    cleanup_requests: int | None = None
    cleanup_seconds: float | None = None

    def check(self):
        # The exact type guards below still reject None; casts only expose that contract to mypy.
        started_at = cast(float, self.started_at)
        cleanup_seconds = cast(float, self.cleanup_seconds)
        if (
            type(self.deadline) not in {int, float}
            or not math.isfinite(self.deadline)
            or self.deadline <= 0
        ):
            raise DriverRefused("invalid_deadline")
        if type(self.requests) is not int or not 0 < self.requests <= 10000:
            raise DriverRefused("invalid_limit")
        # Zero disables this optional resource kind; begin() refuses its first reservation.
        for name in ("projects", "issues", "webhooks", "comments"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 10000:
                raise DriverRefused("invalid_limit")
        if (
            type(self.started_at) not in {int, float}
            or not math.isfinite(started_at)
            or started_at < 0
            or type(self.cleanup_seconds) not in {int, float}
            or not math.isfinite(cleanup_seconds)
            or cleanup_seconds <= 0
            or started_at + cleanup_seconds >= self.deadline
            or type(self.cleanup_requests) is not int
            or not 0 < self.cleanup_requests < self.requests
        ):
            raise DriverRefused("invalid_cleanup_reserve")


@dataclass(frozen=True)
class Scope:
    workspace: str
    owner: str
    team: str
    project: str
    forbidden_project: str

    def check(self):
        for value in asdict(self).values():
            uuid(value)
        if self.project == self.forbidden_project:
            raise DriverRefused("development_project_forbidden")


KINDS = {"project": "projects", "issue": "issues", "webhook": "webhooks", "comment": "comments"}
TARGETS = {
    ("project", "archive"): "archived",
    ("issue", "cancel"): "canceled",
    ("issue", "archive"): "archived",
    ("webhook", "disable"): "disabled",
    ("webhook", "delete"): "deleted",
}
DIAGNOSTIC_FIELDS = {
    "request_id",
    "status",
    "code",
    "raw_sha256",
    "size",
    "seconds",
    "active",
    "late",
    "underlying_status",
    "route_matched",
    "remaining",
    "worked",
}
DIAGNOSTIC_KINDS = {
    "capacity_refused",
    "admission_refused",
    "handler_failed",
    "server_failed",
    "reply_unconfirmed",
    "request_received",
    "adapter_failed",
    "adapter_finished",
    "http_wait_elapsed",
    "drain_failed",
    "drain_wake_completed",
}
DIAGNOSTIC_CODES = {
    "handler_exception",
    "server_exception",
    "response_write_failed",
    "request_budget_or_store_unavailable",
    "adapter_exception",
    "underlying_outcome_pending",
    "drain_exception",
}


class RunLedger:
    def __init__(self, path, run_id, scope, limits, *, clock=time.time):
        uuid(run_id)
        scope.check()
        limits.check()
        self.path, self.run_id, self.scope, self.limits = Path(path), run_id, scope, limits
        self.clock = clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.parent.stat().st_mode & 0o077:
            raise DriverRefused("private_directory_required")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        private_path(self.path)
        expected = json.dumps(
            {"run": run_id, "scope": asdict(scope), "limits": asdict(limits)}, sort_keys=True
        )
        with self.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS meta(value TEXT NOT NULL)")
            rows = conn.execute("SELECT value FROM meta").fetchall()
            if not rows:
                conn.execute("INSERT INTO meta VALUES(?)", (expected,))
                conn.execute(
                    "CREATE TABLE requests(id INTEGER PRIMARY KEY,purpose TEXT NOT NULL,"
                    "cleanup_key TEXT)"
                )
                conn.execute(
                    "CREATE TABLE operations(key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,"
                    "kind TEXT NOT NULL,candidate TEXT UNIQUE NOT NULL,state TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE resources(id TEXT PRIMARY KEY,kind TEXT NOT NULL,"
                    "operation_key TEXT UNIQUE NOT NULL,state TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE observations(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,"
                    "payload TEXT NOT NULL)"
                )
                conn.execute("CREATE TABLE driver_schema(version INTEGER NOT NULL)")
                conn.execute("INSERT INTO driver_schema VALUES(2)")
                conn.execute("CREATE TABLE run_state(phase TEXT NOT NULL)")
                conn.execute("INSERT INTO run_state VALUES('ordinary')")
                conn.execute(
                    "CREATE TABLE cleanup_actions(key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,"
                    "resource_id TEXT NOT NULL,kind TEXT NOT NULL,action TEXT NOT NULL,"
                    "target TEXT NOT NULL,state TEXT NOT NULL,before_digest TEXT,"
                    "result_digest TEXT,attempts INTEGER NOT NULL DEFAULT 0,"
                    "UNIQUE(resource_id,action))"
                )
            else:
                # Old evidence remains readable with its original source. Never invent
                # missing reserve/start provenance or silently reset an older run.
                if conn.execute(
                    "SELECT candidate FROM operations GROUP BY candidate HAVING COUNT(*)>1"
                ).fetchone():
                    raise DriverRefused("legacy_duplicate_reservations")
                if len(rows) != 1 or rows[0][0] != expected:
                    raise DriverRefused("ledger_binding_changed")
                schema = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='driver_schema'"
                ).fetchone()
                if not schema:
                    raise DriverRefused("legacy_binding_requires_review")
                if [row[0] for row in conn.execute("SELECT version FROM driver_schema")] != [2]:
                    raise DriverRefused("unsupported_driver_schema")

    @contextmanager
    def transaction(self):
        private_path(self.path)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=2)
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

    def authorize_recovery_window(self, reference, *, started_at, deadline, cleanup_seconds):
        """Trusted fixture operator amendment; original limits and counters stay immutable."""
        now = self.clock()
        if (
            not isinstance(reference, str)
            or not 1 <= len(reference) <= 256
            or any(
                type(value) not in {int, float} or not math.isfinite(value)
                for value in (started_at, deadline, cleanup_seconds, now)
            )
            or not self.limits.deadline <= started_at <= now < deadline
            or deadline - started_at > 5400
            or not 0 < cleanup_seconds < deadline - now
        ):
            raise DriverRefused("invalid_recovery_window")
        value = dict(
            reference=reference,
            run=self.run_id,
            scope=asdict(self.scope),
            original_limits_digest=digest(asdict(self.limits)),
            started_at=started_at,
            deadline=deadline,
            cleanup_seconds=cleanup_seconds,
        )
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT payload FROM observations WHERE kind='recovery_window'"
            ).fetchall()
            if rows:
                if len(rows) != 1 or rows[0][0] != raw:
                    raise DriverRefused("recovery_window_changed")
                return value
            if conn.execute("SELECT phase FROM run_state").fetchone()[0] != "ordinary":
                raise DriverRefused("cleanup_phase_active")
            conn.execute(
                "INSERT INTO observations(kind,payload) VALUES('recovery_window',?)", (raw,)
            )
        return value

    def _time(self, *, ordinary=False, conn=None):
        if conn is None:
            with self.transaction() as current:
                return self._time(ordinary=ordinary, conn=current)
        rows = conn.execute(
            "SELECT payload FROM observations WHERE kind='recovery_window'"
        ).fetchall()
        start, deadline, reserve = (
            self.limits.started_at,
            self.limits.deadline,
            self.limits.cleanup_seconds,
        )
        if rows:
            if len(rows) != 1:
                raise DriverRefused("recovery_window_changed")
            window = json.loads(rows[0][0])
            if (
                window.get("run") != self.run_id
                or window.get("scope") != asdict(self.scope)
                or window.get("original_limits_digest") != digest(asdict(self.limits))
            ):
                raise DriverRefused("recovery_window_changed")
            start, deadline, reserve = (
                window["started_at"],
                window["deadline"],
                window["cleanup_seconds"],
            )
        now = self.clock()
        if (
            type(now) not in {int, float}
            or not math.isfinite(now)
            or now < start
            or now >= deadline
        ):
            raise DriverRefused("deadline_expired")
        if ordinary and now >= deadline - reserve:
            raise DriverRefused("ordinary_deadline_expired")
        return deadline

    def request_deadline(self):
        """Verified effective hard cutoff; request charging and reserve stay separate."""
        return self._time()

    def _ordinary(self, conn):
        self._time(ordinary=True, conn=conn)
        if conn.execute("SELECT phase FROM run_state").fetchone()[0] != "ordinary":
            raise DriverRefused("cleanup_phase_active")

    def begin(self, key, kind, candidate, content_digest):
        if kind not in KINDS or not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise DriverRefused("invalid_operation")
        uuid(candidate)
        if not isinstance(content_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", content_digest):
            raise DriverRefused("invalid_content_digest")
        fingerprint = digest({"kind": kind, "candidate": candidate, "digest": content_digest})
        with self.transaction() as conn:
            prior = conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DriverRefused("operation_key_collision")
                return dict(prior)
            self._ordinary(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM operations WHERE kind=?", (kind,)
            ).fetchone()[0]
            if (
                kind == "issue"
                and conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_observations'"
                ).fetchone()
            ):
                count += conn.execute("SELECT COUNT(*) FROM product_observations").fetchone()[0]
            if count >= getattr(self.limits, KINDS[kind]):
                raise DriverRefused("resource_budget_exhausted")
            if conn.execute("SELECT 1 FROM operations WHERE candidate=?", (candidate,)).fetchone():
                raise DriverRefused("candidate_already_reserved")
            conn.execute(
                "INSERT INTO operations VALUES(?,?,?,?,'intent')",
                (key, fingerprint, kind, candidate),
            )
            return dict(conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone())

    @contextmanager
    def ordinary_request(self, purpose):
        """Charge and commit a trusted fixture action in the same transaction."""
        if purpose not in {"create", "read", "product_transport", "receiver_http"}:
            raise DriverRefused("unknown_request_purpose")
        with self.transaction() as conn:
            self._ordinary(conn)
            used = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE cleanup_key IS NULL"
            ).fetchone()[0]
            if used >= self.limits.requests - self.limits.cleanup_requests:
                raise DriverRefused("request_budget_exhausted")
            conn.execute("INSERT INTO requests(purpose) VALUES(?)", (purpose,))
            yield conn

    def request(self, purpose):
        with self.ordinary_request(purpose):
            pass

    def charged_create_send(self, key):
        with self.ordinary_request("create") as conn:
            self._mark_sent(conn, key)

    def get(self, key):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if row is None:
                raise DriverRefused("intent_missing")
            return dict(row)

    def mark_sent(self, key):
        with self.transaction() as conn:
            self._ordinary(conn)
            self._mark_sent(conn, key)

    @staticmethod
    def _mark_sent(conn, key):
        changed = conn.execute(
            "UPDATE operations SET state='sent' WHERE key=? AND state='intent'", (key,)
        ).rowcount
        if changed != 1:
            raise DriverRefused("intent_not_sendable")

    def uncertain(self, key):
        with self.transaction() as conn:
            if (
                conn.execute(
                    "UPDATE operations SET state='uncertain' WHERE key=? "
                    "AND state IN ('sent','uncertain')",
                    (key,),
                ).rowcount
                != 1
            ):
                raise DriverRefused("sent_intent_missing")

    def abandon_create(self, key):
        with self.transaction() as conn:
            row = conn.execute("SELECT state FROM operations WHERE key=?", (key,)).fetchone()
            if not row or row[0] not in {"intent", "abandoned"}:
                raise DriverRefused("only_unsent_create_can_be_abandoned")
            conn.execute("UPDATE operations SET state='abandoned' WHERE key=?", (key,))

    def confirm(self, key, resource_id, kind):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if not row or row["state"] not in {"sent", "uncertain", "confirmed"}:
                raise DriverRefused("sent_intent_missing")
            if (row["candidate"], row["kind"]) != (resource_id, kind):
                raise DriverRefused("readback_identity_changed")
            conn.execute(
                "INSERT OR IGNORE INTO resources VALUES(?,?,?,'active')", (resource_id, kind, key)
            )
            existing = conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if (existing["kind"], existing["operation_key"]) != (kind, key):
                raise DriverRefused("resource_already_owned")
            conn.execute("UPDATE operations SET state='confirmed' WHERE key=?", (key,))

    def observe_product_issue(self, key, issue_id, provenance):
        """Append verified post-effect ownership, never a fixture create/send reservation."""
        uuid(issue_id)
        if not isinstance(key, str) or not 1 <= len(key) <= 256 or type(provenance) is not dict:
            raise DriverRefused("invalid_product_observation")
        raw = json.dumps(provenance, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 262144:
            raise DriverRefused("product_observation_too_large")
        fingerprint = digest(
            {
                "run": self.run_id,
                "scope": asdict(self.scope),
                "issue": issue_id,
                "provenance": provenance,
            }
        )
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS product_observations("
                "key TEXT PRIMARY KEY,issue_id TEXT UNIQUE NOT NULL,"
                "fingerprint TEXT NOT NULL,provenance TEXT NOT NULL,"
                "observed_at REAL NOT NULL,origin TEXT NOT NULL)"
            )
            prior = conn.execute(
                "SELECT * FROM product_observations WHERE key=?", (key,)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DriverRefused("product_observation_changed")
                resource = conn.execute(
                    "SELECT * FROM resources WHERE id=?", (issue_id,)
                ).fetchone()
                if not resource or (resource["kind"], resource["operation_key"]) != ("issue", key):
                    raise DriverRefused("observed_product_resource_changed")
                return dict(resource)
            self._ordinary(conn)
            count = conn.execute("SELECT COUNT(*) FROM operations WHERE kind='issue'").fetchone()[0]
            count += conn.execute("SELECT COUNT(*) FROM product_observations").fetchone()[0]
            if count >= self.limits.issues:
                raise DriverRefused("resource_budget_exhausted")
            if (
                conn.execute(
                    "SELECT 1 FROM operations WHERE candidate=? OR key=?", (issue_id, key)
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM resources WHERE id=? OR operation_key=?", (issue_id, key)
                ).fetchone()
            ):
                raise DriverRefused("resource_already_owned")
            conn.execute(
                "INSERT INTO product_observations VALUES(?,?,?,?,?,?)",
                (key, issue_id, fingerprint, raw, self.clock(), "confirmed_product_post_effect"),
            )
            conn.execute("INSERT INTO resources VALUES(?,'issue',?,'active')", (issue_id, key))
        return self.resource(issue_id)

    def resource(self, resource_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                raise DriverRefused("owned_uuid_missing")
            return dict(row)

    def cleaned(self, resource_id, state):
        raise DriverRefused("cleanup_action_intent_required")

    def begin_cleanup(self, key, resource_id, action, *, expected_state=None):
        uuid(resource_id)
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise DriverRefused("invalid_cleanup_key")
        with self.transaction() as conn:
            resource = conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not resource:
                raise DriverRefused("owned_uuid_missing")
            target = TARGETS.get((resource["kind"], action))
            if target is None:
                raise DriverRefused("cleanup_action_forbidden")
            if expected_state is not None and expected_state != target:
                raise DriverRefused("cleanup_target_changed")
            fingerprint = digest(
                {
                    "run": self.run_id,
                    "scope": asdict(self.scope),
                    "id": resource_id,
                    "kind": resource["kind"],
                    "action": action,
                    "target": target,
                }
            )
            prior = conn.execute("SELECT * FROM cleanup_actions WHERE key=?", (key,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DriverRefused("cleanup_key_collision")
                return dict(prior)
            self._time(conn=conn)
            if resource["kind"] == "project":
                self._closed_children(conn, exclude_cleanup_key=key)
            if conn.execute(
                "SELECT 1 FROM cleanup_actions WHERE resource_id=? AND action=?",
                (resource_id, action),
            ).fetchone():
                raise DriverRefused("cleanup_action_already_reserved")
            conn.execute(
                "INSERT INTO cleanup_actions(key,fingerprint,resource_id,kind,action,target,state)"
                " VALUES(?,?,?,?,?,?,'intent')",
                (key, fingerprint, resource_id, resource["kind"], action, target),
            )
            conn.execute("UPDATE run_state SET phase='cleanup'")
            return dict(
                conn.execute("SELECT * FROM cleanup_actions WHERE key=?", (key,)).fetchone()
            )

    def cleanup_action(self, key):
        with self.transaction() as conn:
            return dict(self._cleanup_row(conn, key))

    def _cleanup_row(self, conn, key):
        row = conn.execute("SELECT * FROM cleanup_actions WHERE key=?", (key,)).fetchone()
        if row is None:
            raise DriverRefused("cleanup_intent_missing")
        resource = conn.execute(
            "SELECT * FROM resources WHERE id=?", (row["resource_id"],)
        ).fetchone()
        if not resource or resource["kind"] != row["kind"]:
            raise DriverRefused("cleanup_ownership_changed")
        return row

    def _cleanup_request(self, conn, key, purpose):
        self._time(conn=conn)
        row = self._cleanup_row(conn, key)
        if row["state"] not in {"intent", "sent", "uncertain"}:
            raise DriverRefused("cleanup_already_confirmed")
        self._reserved_charge(conn, key, purpose)

    def _reserved_charge(self, conn, key, purpose):
        total = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        used = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE cleanup_key IS NOT NULL"
        ).fetchone()[0]
        if total >= self.limits.requests or used >= self.limits.cleanup_requests:
            raise DriverRefused("cleanup_budget_exhausted")
        conn.execute("INSERT INTO requests(purpose,cleanup_key) VALUES(?,?)", (purpose, key))

    def create_recovery_read(self, key):
        """Only a durable possible create may borrow reserved recovery reads."""
        with self.transaction() as conn:
            self._time(conn=conn)
            row = conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if not row or row["state"] not in {"sent", "uncertain"}:
                raise DriverRefused("possible_create_intent_required")
            self._reserved_charge(conn, "create-recovery:" + key, "create_recovery_read")

    def cleanup_read(self, key):
        with self.transaction() as conn:
            self._cleanup_request(conn, key, "cleanup_read")

    def cleanup_prepared(self, key, before_digest):
        self._digest(before_digest)
        with self.transaction() as conn:
            self._time(conn=conn)
            row = self._cleanup_row(conn, key)
            if row["state"] != "intent":
                raise DriverRefused("cleanup_not_preparable")
            if not conn.execute(
                "SELECT 1 FROM requests WHERE cleanup_key=? AND purpose='cleanup_read'", (key,)
            ).fetchone():
                raise DriverRefused("cleanup_read_intent_missing")
            conn.execute(
                "UPDATE cleanup_actions SET before_digest=? WHERE key=?", (before_digest, key)
            )

    def cleanup_sent(self, key):
        with self.transaction() as conn:
            row = self._cleanup_row(conn, key)
            if row["state"] != "intent" or row["before_digest"] is None:
                raise DriverRefused("cleanup_not_sendable")
            self._cleanup_request(conn, key, "cleanup_effect")
            conn.execute(
                "UPDATE cleanup_actions SET state='sent',attempts=attempts+1 WHERE key=?", (key,)
            )

    def cleanup_uncertain(self, key):
        with self.transaction() as conn:
            if self._cleanup_row(conn, key)["state"] == "confirmed":
                return  # A concurrent exact readback cannot be downgraded.
            if (
                conn.execute(
                    "UPDATE cleanup_actions SET state='uncertain' WHERE key=? "
                    "AND state IN ('sent','uncertain')",
                    (key,),
                ).rowcount
                != 1
            ):
                raise DriverRefused("cleanup_sent_intent_missing")

    def cleanup_confirm(self, key, result_digest):
        self._digest(result_digest)
        with self.transaction() as conn:
            row = self._cleanup_row(conn, key)
            if row["state"] == "confirmed" and row["result_digest"] == result_digest:
                return
            if row["state"] not in {"sent", "uncertain"} or row["attempts"] != 1:
                raise DriverRefused("cleanup_sent_intent_missing")
            conn.execute(
                "UPDATE cleanup_actions SET state='confirmed',result_digest=? WHERE key=?",
                (result_digest, key),
            )
            conn.execute(
                "UPDATE resources SET state=? WHERE id=?", (row["target"], row["resource_id"])
            )

    @staticmethod
    def _digest(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise DriverRefused("invalid_content_digest")

    def require_closed_children(self, *, exclude_cleanup_key=""):
        with self.transaction() as conn:
            self._closed_children(conn, exclude_cleanup_key=exclude_cleanup_key)

    @staticmethod
    def _closed_children(conn, *, exclude_cleanup_key=""):
        if conn.execute(
            "SELECT 1 FROM operations WHERE state NOT IN ('confirmed','abandoned')"
        ).fetchone():
            raise DriverRefused("unresolved_effects_retained")
        if conn.execute(
            "SELECT 1 FROM cleanup_actions WHERE state!='confirmed' AND key!=?",
            (exclude_cleanup_key,),
        ).fetchone():
            raise DriverRefused("unresolved_cleanup_retained")
        if conn.execute(
            "SELECT 1 FROM resources WHERE kind='issue' AND state NOT IN "
            "('archived','canceled') OR kind='webhook' AND state NOT IN "
            "('disabled','deleted')"
        ).fetchone():
            raise DriverRefused("owned_children_not_closed")

    def observe(self, kind, **fields):
        if kind not in DIAGNOSTIC_KINDS or set(fields) - DIAGNOSTIC_FIELDS:
            raise DriverRefused("unsafe_diagnostic_fields")
        for key, value in fields.items():
            if key == "request_id":
                uuid(value)
            elif key == "code" and value not in DIAGNOSTIC_CODES:
                raise DriverRefused("unsafe_diagnostic_code")
            elif key == "raw_sha256" and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise DriverRefused("invalid_digest")
            elif key not in {"request_id", "code", "raw_sha256"} and (
                type(value) not in {int, float, bool} or not math.isfinite(value)
            ):
                raise DriverRefused("unsafe_diagnostic_value")
        with self.transaction() as conn:
            if (
                conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                >= self.limits.requests * 8
            ):
                raise DriverRefused("diagnostic_budget_exhausted")
            conn.execute(
                "INSERT INTO observations(kind,payload) VALUES(?,?)",
                (kind, json.dumps(fields, sort_keys=True)),
            )

    def summary(self):
        with self.transaction() as conn:
            tables = ["operations", "resources", "requests", "observations", "cleanup_actions"]
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_observations'"
            ).fetchone():
                tables.append("product_observations")
            return {
                table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in tables
            }
