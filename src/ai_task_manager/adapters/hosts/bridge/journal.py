"""A separate signed host journal; it never owns task or provider state."""

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_task_manager.config.files import digest, private_path
from ai_task_manager.domains.work.errors import AccessDenied, SchemaError

PREFIX = "host-record:"
KINDS = frozenset(
    {
        "startup",
        "enrollment",
        "delegated_enrollment",
        "human",
        "notification",
        "notification_input",
        "trace",
        "launch",
        "rotation",
    }
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class HostJournal:
    @classmethod
    def open_existing(cls, path, key_path, *, clock=time.time):
        """Read an enrolled producer store without creating or migrating its files."""
        value = cls.__new__(cls)
        value.path, value.key_path = private_path(path), private_path(key_path)
        for parent in {value.path.parent, value.key_path.parent}:
            if parent.is_symlink() or parent.stat().st_mode & 0o077:
                raise AccessDenied("Host storage needs a private owned directory")
        value.clock = clock
        value._key = value.key_path.read_bytes()
        if len(value._key) != 32:
            raise AccessDenied("Invalid host journal key")
        return value

    def __init__(self, path, key_path, *, clock=time.time):
        self.path, self.key_path, self.clock = (
            Path(path).absolute(),
            Path(key_path).absolute(),
            clock,
        )
        for parent in {self.path.parent, self.key_path.parent}:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if parent.is_symlink() or parent.stat().st_mode & 0o077:
                raise AccessDenied("Host storage needs a private owned directory")
        private_path(self.path, existing=False)
        private_path(self.key_path, existing=False)
        try:
            with self.key_path.open("xb") as output:
                os.chmod(self.key_path, 0o600)
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            pass
        self._key = private_path(self.key_path).read_bytes()
        if len(self._key) != 32:
            raise AccessDenied("Invalid host journal key")
        with self.transaction() as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and tables != {"host_version", "host_records", "host_intents"}:
                raise SchemaError("Host journal cannot use a foreign authority store")
            conn.execute("CREATE TABLE IF NOT EXISTS host_version(version INTEGER NOT NULL)")
            rows = conn.execute("SELECT version FROM host_version").fetchall()
            if not rows:
                conn.execute("INSERT INTO host_version VALUES(2)")
            elif len(rows) != 1 or rows[0][0] not in {1, 2}:
                raise SchemaError("Unsupported host journal")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS host_records("
                "ref TEXT PRIMARY KEY, data TEXT NOT NULL, mac TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS host_intents("
                "identity TEXT PRIMARY KEY, data TEXT NOT NULL, mac TEXT)"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(host_intents)")}
            if "mac" not in columns:
                # Old unsigned intents remain unresolved. Never manufacture provenance
                # by signing historical data whose original integrity is unknown.
                conn.execute("ALTER TABLE host_intents ADD COLUMN mac TEXT")
            conn.execute("UPDATE host_version SET version=2")
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

    def _append(self, kind, payload, *, sources=(), expires_at=None):
        if kind not in KINDS or (
            expires_at is not None
            and (
                isinstance(expires_at, bool)
                or not math.isfinite(expires_at)
                or expires_at <= self.clock()
            )
        ):
            raise AccessDenied("Invalid host receipt lifetime or kind")
        source_data = [self._read(ref) for ref in sources]
        if source_data:
            limits = [x["expires_at"] for x in source_data if x["expires_at"] is not None]
            if limits:
                expires_at = min([expires_at] + limits) if expires_at is not None else min(limits)
        ref = PREFIX + str(uuid4())
        data = {
            "ref": ref,
            "kind": kind,
            "payload": payload,
            "sources": list(sources),
            "source_hashes": [digest(x) for x in source_data],
            "observed_at": self.clock(),
            "expires_at": expires_at,
        }
        raw = canonical(data)
        mac = hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()
        with self.transaction() as conn:
            conn.execute("INSERT INTO host_records VALUES(?,?,?)", (ref, raw, mac))
        return ref

    def _read(self, ref, kind=None, *, fresh=True):
        if not isinstance(ref, str) or not ref.startswith(PREFIX):
            raise AccessDenied("Unknown host receipt issuer")
        with self.transaction() as conn:
            row = conn.execute("SELECT data,mac FROM host_records WHERE ref=?", (ref,)).fetchone()
        if row is None or not hmac.compare_digest(
            row[1], hmac.new(self._key, row[0].encode(), hashlib.sha256).hexdigest()
        ):
            raise AccessDenied("Host receipt is missing or changed")
        data = json.loads(row[0])
        if (
            data["ref"] != ref
            or data["kind"] not in KINDS
            or (kind is not None and data["kind"] != kind)
            or (fresh and data["expires_at"] is not None and data["expires_at"] <= self.clock())
        ):
            raise AccessDenied("Host receipt is stale or belongs to another observation")
        for source, expected in zip(data["sources"], data["source_hashes"], strict=True):
            if digest(self._read(source, fresh=False)) != expected:
                raise AccessDenied("Host source changed")
        return data

    def _intent_mac(self, identity, raw):
        return hmac.new(
            self._key, ("intent:" + identity + "\0" + raw).encode(), hashlib.sha256
        ).hexdigest()

    def _decode_intent(self, identity, raw, mac):
        if not mac or not hmac.compare_digest(mac, self._intent_mac(identity, raw)):
            raise AccessDenied("Host intent is unsigned, changed or unresolved")
        return json.loads(raw)

    def intend(self, identity, value):
        """An existing launch/rotation intent never authorizes performing it twice."""
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise AccessDenied("A bounded immutable invocation identity is required")
        raw = canonical(value)
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT data,mac FROM host_intents WHERE identity=?", (identity,)
            ).fetchone()
            if old is not None:
                self._decode_intent(identity, old[0], old[1])
                if old[0] != raw:
                    raise AccessDenied("Intent identity was reused with different bindings")
                return False
            conn.execute(
                "INSERT INTO host_intents VALUES(?,?,?)",
                (identity, raw, self._intent_mac(identity, raw)),
            )
            return True

    def intent(self, identity):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT data,mac FROM host_intents WHERE identity=?", (identity,)
            ).fetchone()
        return self._decode_intent(identity, row[0], row[1]) if row else None

    def _history(self, kind):
        with self.transaction() as conn:
            refs = [r[0] for r in conn.execute("SELECT ref FROM host_records ORDER BY rowid")]
        return [data for ref in refs if (data := self._read(ref, fresh=False))["kind"] == kind]

    def _intents(self):
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT identity,data,mac FROM host_intents ORDER BY rowid"
            ).fetchall()
        return [(identity, self._decode_intent(identity, raw, mac)) for identity, raw, mac in rows]
