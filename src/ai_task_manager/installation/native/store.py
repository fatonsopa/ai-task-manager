"""Private normal invocation facts; task policy and global capacity stay in Core."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import local
from uuid import uuid4

from ai_task_manager.config.files import digest, private_path
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.coherence import (
    StoreBinding,
    is_busy_sqlite,
    probe_connection,
    probe_read_connection,
)


class NativeStore:
    def __init__(self, path, binding):
        self.path = Path(path).absolute()
        self._active = local()
        self.binding = StoreBinding(
            self.path, binding["device"], binding["inode"], "native_meta", 3, binding["store_id"]
        )
        with self.transaction():
            pass

    @classmethod
    def create(cls, path):
        path = Path(path).absolute()
        private_path(path, existing=False)
        with path.open("xb"):
            path.chmod(0o600)
        try:
            with sqlite3.connect(path) as conn:
                conn.executescript(
                    "CREATE TABLE native_meta(version INTEGER,store_id TEXT);"
                    "CREATE TABLE native_permissions(reference TEXT PRIMARY KEY,data TEXT);"
                    "CREATE TABLE native_calls(call_id TEXT PRIMARY KEY,"
                    "permission_ref TEXT NOT NULL,"
                    "attempt_id TEXT NOT NULL,purpose TEXT NOT NULL,"
                    "status TEXT NOT NULL,process_stopped INTEGER NOT NULL "
                    "CHECK(process_stopped IN(0,1)),data TEXT NOT NULL);"
                    "CREATE INDEX native_calls_permission ON native_calls(permission_ref);"
                    "CREATE INDEX native_calls_purpose ON "
                    "native_calls(permission_ref,attempt_id,purpose);"
                    "CREATE TABLE native_conformance("
                    "reference TEXT PRIMARY KEY,data TEXT NOT NULL);"
                    "CREATE INDEX native_calls_active ON "
                    "native_calls(permission_ref,process_stopped);"
                    "CREATE TABLE native_events(sequence INTEGER PRIMARY KEY,kind TEXT,"
                    "call_id TEXT,data TEXT);"
                )
                identity = str(uuid4())
                conn.execute("INSERT INTO native_meta VALUES(3,?)", (identity,))
            info = path.stat()
            return cls(path, {"device": info.st_dev, "inode": info.st_ino, "store_id": identity})
        except BaseException:
            # A partially created store is retained for operator diagnosis, never overwritten.
            raise

    @property
    def pointer(self):
        b = self.binding
        return {
            "path": str(self.path),
            "device": b.device,
            "inode": b.inode,
            "store_id": b.store_id,
        }

    @contextmanager
    def settlement_snapshot(self):
        if getattr(self._active, "connection", None) is not None:
            raise AccessDenied("Settlement cannot wait inside its own native transaction")
        private_path(self.path)
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            conn.execute("BEGIN")
            probe_read_connection(conn, self.binding)
            yield conn
            probe_read_connection(conn, self.binding)
        finally:
            conn.rollback()
            conn.close()

    @contextmanager
    def identity_acquisition(self, *, not_after, clock, cancelled, validate):
        if getattr(self._active, "connection", None) is not None or getattr(
            self._active, "identity_wait", None
        ) is not None:
            raise AccessDenied("Identity admission cannot nest an owned native transaction")
        policy = (
            not_after, clock, cancelled, time.monotonic() + max(0, not_after - clock()), validate
        )
        self._active.identity_wait = policy
        try:
            yield
        finally:
            del self._active.identity_wait

    @contextmanager
    def transaction(self, *, wait_until=None, clock=None, cancelled=None):
        private_path(self.path)
        identity = getattr(self._active, "identity_wait", None)
        if identity is not None:
            original_end, original_clock, original_cancel, monotonic_end, validate = identity
            wait_until = min(wait_until, original_end) if wait_until is not None else original_end
            clock, cancelled = original_clock, original_cancel
        # Only trusted settlement or scoped identity admission supplies a bound.
        # Ordinary admission/effect acquisition retains its fail-fast SQLite policy.
        conn = sqlite3.connect(self.path, timeout=0 if wait_until is not None else 0.1)
        end = time.monotonic() + max(0, wait_until - clock()) if wait_until is not None else None
        if identity is not None:
            end = min(end, monotonic_end)
        try:
            if wait_until is not None and getattr(self._active, "connection", None) is not None:
                raise AccessDenied("Settlement cannot wait inside its own native transaction")
            contended = False
            while True:
                if (contended or identity is not None) and (
                    clock() >= wait_until or time.monotonic() >= end
                ):
                    raise AccessDenied("Original native settlement wait expired")
                if cancelled is not None and cancelled.is_set():
                    raise AccessDenied("Native settlement wait was cancelled")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.Error as error:
                    if wait_until is None or not is_busy_sqlite(error):
                        raise
                    contended = True
                    remaining = min(wait_until - clock(), end - time.monotonic())
                    if remaining <= 0:
                        raise AccessDenied("Original native settlement wait expired") from error
                    # No transaction, writer, model guard or Core lock is held.
                    if cancelled is not None:
                        cancelled.wait(min(0.02, remaining))
                    else:
                        time.sleep(min(0.02, remaining))
            probe_connection(conn, self.binding)
            self._active.connection = conn
            if identity is not None:
                validate(conn)
            yield conn
            if identity is not None:
                validate(conn)
            probe_connection(conn, self.binding)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            if getattr(self._active, "connection", None) is conn:
                del self._active.connection
            conn.close()

    @staticmethod
    def read(conn, table, key):
        if table not in {"native_permissions", "native_calls", "native_conformance"}:
            raise AccessDenied("Unknown normal invocation fact")
        column = "call_id" if table == "native_calls" else "reference"
        row = conn.execute(f"SELECT data FROM {table} WHERE {column}=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(conn, table, key, value):
        if table not in {"native_permissions", "native_calls", "native_conformance"}:
            raise AccessDenied("Unknown normal invocation fact")
        column = "call_id" if table == "native_calls" else "reference"
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if table == "native_calls":
            if type(value["process_stopped"]) is not bool:
                raise AccessDenied("Explicit native process stop fact required")
            conn.execute(
                "INSERT INTO native_calls VALUES(?,?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET "
                "permission_ref=excluded.permission_ref,attempt_id=excluded.attempt_id,purpose=excluded.purpose,status=excluded.status,"
                "process_stopped=excluded.process_stopped,data=excluded.data",
                (
                    key,
                    value["permission_ref"],
                    value["metadata"]["invocation_binding"]["attempt_id"],
                    value["metadata"]["invocation_binding"]["purpose"],
                    value["status"],
                    int(value["process_stopped"]),
                    raw,
                ),
            )
        else:
            conn.execute(
                f"INSERT INTO {table}({column},data) VALUES(?,?) "
                f"ON CONFLICT({column}) DO UPDATE SET data=excluded.data",
                (key, raw),
            )

    @staticmethod
    def event(conn, kind, call_id, value):
        conn.execute(
            "INSERT INTO native_events(kind,call_id,data) VALUES(?,?,?)",
            (kind, call_id, json.dumps(value, sort_keys=True, allow_nan=False)),
        )

    def conformance(self, reference):
        """Read capability state under this store's exact owned effect transaction."""
        active = getattr(self._active, "connection", None)
        if active is not None:
            probe_connection(active, self.binding)
            return self.read(active, "native_conformance", reference)
        with self.transaction() as conn:
            return self.read(conn, "native_conformance", reference)

    def permission(self, reference):
        # A managed canary rechecks authority while its invocation owns the
        # writer transaction. Read that exact same-thread/store transaction;
        # opening another writer would deadlock against our own effect guard.
        active = getattr(self._active, "connection", None)
        if active is not None:
            probe_connection(active, self.binding)
            value = self.read(active, "native_permissions", reference)
        else:
            with self.transaction() as conn:
                value = self.read(conn, "native_permissions", reference)
        if value is None:
            raise AccessDenied("Normal native permission was not enrolled")
        return value

    def revoke(self, reference):
        with self.transaction() as conn:
            value = self.read(conn, "native_permissions", reference)
            if value is None:
                raise AccessDenied("Unknown normal permission")
            value["revoked"] = True
            self.put(conn, "native_permissions", reference, value)
            self.event(conn, "permission_revoked", None, {"reference": reference})

    def enroll(self, reference, value):
        with self.transaction() as conn:
            old = self.read(conn, "native_permissions", reference)
            if old is not None and digest(old) != digest(value):
                raise AccessDenied("Normal enrollment cannot replace existing authority")
            if old is None:
                self.put(conn, "native_permissions", reference, value)
                self.event(conn, "permission_enrolled", None, {"reference": reference})
