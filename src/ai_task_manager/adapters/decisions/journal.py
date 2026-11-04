"""Source-bound signed records owned by the application authority."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.config.files import private_path
from ai_task_manager.domains.work.errors import AccessDenied, SchemaError
from ai_task_manager.domains.work.models import Evidence, Scope


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class AuthorityJournal:
    def __init__(self, path, key_path, *, clock=time.time):
        self.path, self.key_path, self.clock = Path(path), Path(key_path), clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in {self.path.parent, self.key_path.parent}:
            if parent.is_symlink() or parent.stat().st_mode & 0o077:
                raise AccessDenied("Authority storage requires a private owned directory")
        private_path(self.path, existing=False)
        private_path(self.key_path, existing=False)
        try:
            with self.key_path.open("xb") as stream:
                os.chmod(self.key_path, 0o600)
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        self._key = private_path(self.key_path).read_bytes()
        if len(self._key) != 32:
            raise AccessDenied("Authority key is invalid")
        with self.transaction() as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not tables:
                conn.execute("CREATE TABLE authority_version(version INTEGER NOT NULL)")
                conn.execute("INSERT INTO authority_version VALUES(2)")
                conn.execute(
                    "CREATE TABLE records(ref TEXT PRIMARY KEY, data TEXT NOT NULL, "
                    "mac TEXT NOT NULL)"
                )
            else:
                try:
                    versions = conn.execute("SELECT version FROM authority_version").fetchall()
                except sqlite3.Error as exc:
                    raise SchemaError("Foreign authority store") from exc
                if len(versions) != 1 or versions[0][0] not in {1, 2}:
                    raise SchemaError("Unsupported authority store")
                if versions[0][0] == 1:
                    conn.execute("UPDATE authority_version SET version=2")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS invocations (id TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, context TEXT NOT NULL, result TEXT)"
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _put(self, prefix, data):
        ref = prefix + str(uuid4())
        raw = canonical({**data, "ref": ref})
        mac = hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()
        with self.transaction() as conn:
            conn.execute("INSERT INTO records VALUES(?,?,?)", (ref, raw, mac))
        return ref

    def _get(self, ref, prefix):
        if not isinstance(ref, str) or not ref.startswith(prefix):
            raise AccessDenied("Receipt authority is not available")
        with self.transaction() as conn:
            row = conn.execute("SELECT data,mac FROM records WHERE ref=?", (ref,)).fetchone()
        if row is None or not hmac.compare_digest(
            row[1], hmac.new(self._key, row[0].encode(), hashlib.sha256).hexdigest()
        ):
            raise AccessDenied("Receipt is missing or changed")
        data = json.loads(row[0])
        if data.get("ref") != ref or (
            data.get("expires_at") is not None and data["expires_at"] <= self.clock()
        ):
            raise AccessDenied("Receipt is stale or invalid")
        return data

    def verify_evidence(self, ref):
        data = self._get(ref, "decision-proof:")
        # A durable uncommitted invocation may reuse its saved decision ref.
        # Revalidate its actual delegation at consumption, not only issuance.
        from .observations import PREFIX, current_enrollment

        origin = data.get("source_ref")
        if isinstance(origin, str) and origin.startswith("session-host-receipt:"):
            from .host_observations import verify_receipt

            observed = verify_receipt(self, origin)
            if (data["kind"], data["scope"], data["subject"]) != (
                "notification_" + observed.stage,
                asdict(observed.scope),
                observed.event_id,
            ) or data["payload"] != {
                "target_session_id": observed.session_id,
                "trace_hash": observed.trace_hash,
            }:
                raise AccessDenied("Notification proof differs from its original native source")
        if (
            data["kind"] == "decision"
            and data["payload"].get("actor") == "agent"
            and isinstance(origin, str)
            and origin.startswith(PREFIX)
        ):
            current_enrollment(self, origin, data["scope"])
        if data["kind"] == "delivered_followup":
            if not isinstance(origin, str) or not origin.startswith(PREFIX):
                raise AccessDenied("Delivered follow-up requires enrolled delegation")
            enrollment = current_enrollment(self, origin, data["scope"])
            if "delivered_followup" not in enrollment["actions"]:
                raise AccessDenied("Enrollment does not authorize delivered follow-up")
        return Evidence(
            data["kind"],
            Scope(**data["scope"]),
            data["subject"],
            data["payload"],
            data.get("expires_at"),
        )

    def bind_claim_store(self, payload):
        """One immutable installer binding, serialized with other authority writers."""
        ref = "claim-store:installation"
        raw = canonical({**payload, "ref": ref})
        mac = hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()
        with self.transaction() as conn:
            prior = conn.execute("SELECT data,mac FROM records WHERE ref=?", (ref,)).fetchone()
            if prior is not None:
                if not hmac.compare_digest(
                    prior[1], hmac.new(self._key, prior[0].encode(), hashlib.sha256).hexdigest()
                ):
                    raise AccessDenied("Claim-store enrollment was changed")
                old = json.loads(prior[0])
                if any(old.get(key) != payload.get(key) for key in ("store_id", "file_identity")):
                    raise AccessDenied("A different claim store is already enrolled")
                return {key: value for key, value in old.items() if key != "ref"}
            conn.execute("INSERT INTO records VALUES(?,?,?)", (ref, raw, mac))
        return payload

    def claim_store(self):
        row = self._get("claim-store:installation", "claim-store:")
        if set(row) != {"ref", "installation_id", "store_id", "file_identity"}:
            raise AccessDenied("Claim-store enrollment is invalid")
        return {key: value for key, value in row.items() if key != "ref"}

    def _evidence(self, kind, scope, subject, payload, source_ref, expires_at=None):
        if not source_ref:
            raise AccessDenied("Observed authorization source is required")
        return self._put(
            "decision-proof:",
            {
                "kind": kind,
                "scope": asdict(scope),
                "subject": subject,
                "payload": payload,
                "source_ref": source_ref,
                "expires_at": expires_at,
            },
        )
