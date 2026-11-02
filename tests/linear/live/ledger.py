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

    def check(self):
        if (
            type(self.deadline) not in {int, float}
            or not math.isfinite(self.deadline)
            or self.deadline <= 0
        ):
            raise DriverRefused("invalid_deadline")
        for name in ("requests", "projects", "issues", "webhooks", "comments"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= 10000:
                raise DriverRefused("invalid_limit")


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
                conn.execute("CREATE TABLE requests(id INTEGER PRIMARY KEY, purpose TEXT NOT NULL)")
                conn.execute(
                    "CREATE TABLE operations(key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,"
                    "kind TEXT NOT NULL,candidate TEXT NOT NULL,state TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE resources(id TEXT PRIMARY KEY,kind TEXT NOT NULL,"
                    "operation_key TEXT UNIQUE NOT NULL,state TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE observations(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,"
                    "payload TEXT NOT NULL)"
                )
            elif len(rows) != 1 or rows[0][0] != expected:
                raise DriverRefused("ledger_binding_changed")

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

    def _time(self):
        now = self.clock()
        if type(now) not in {int, float} or not math.isfinite(now) or now >= self.limits.deadline:
            raise DriverRefused("deadline_expired")

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
            self._time()
            count = conn.execute(
                "SELECT COUNT(*) FROM operations WHERE kind=?", (kind,)
            ).fetchone()[0]
            if count >= getattr(self.limits, KINDS[kind]):
                raise DriverRefused("resource_budget_exhausted")
            conn.execute(
                "INSERT INTO operations VALUES(?,?,?,?,'intent')",
                (key, fingerprint, kind, candidate),
            )
            return dict(conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone())

    def request(self, purpose):
        if purpose not in {"create", "read", "cleanup", "product_transport", "receiver_http"}:
            raise DriverRefused("unknown_request_purpose")
        with self.transaction() as conn:
            self._time()
            if conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= self.limits.requests:
                raise DriverRefused("request_budget_exhausted")
            conn.execute("INSERT INTO requests(purpose) VALUES(?)", (purpose,))

    def get(self, key):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if row is None:
                raise DriverRefused("intent_missing")
            return dict(row)

    def mark_sent(self, key):
        with self.transaction() as conn:
            self._time()
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

    def resource(self, resource_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if not row:
                raise DriverRefused("owned_uuid_missing")
            return dict(row)

    def cleaned(self, resource_id, state):
        if state not in {"archived", "canceled", "disabled", "deleted"}:
            raise DriverRefused("invalid_cleanup_state")
        with self.transaction() as conn:
            if (
                conn.execute(
                    "UPDATE resources SET state=? WHERE id=?", (state, resource_id)
                ).rowcount
                != 1
            ):
                raise DriverRefused("owned_uuid_missing")

    def require_closed_children(self):
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM operations WHERE state!='confirmed'").fetchone():
                raise DriverRefused("unresolved_effects_retained")
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
            return {
                table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("operations", "resources", "requests", "observations")
            }
