"""Owned schema-1 before-images and rollback before any schema-2 work is written."""

import hashlib
import os
import sqlite3
from pathlib import Path

from ai_task_manager.config.files import private_path, read_private, write_private
from ai_task_manager.domains.checks.models import canonical, fingerprint
from ai_task_manager.domains.work.errors import AccessDenied, SchemaError
from ai_task_manager.storage.coherence import StoreBinding, probe_connection

LEGACY_TABLES = {"workflow_meta", "runs", "steps", "lineage"}
CURRENT_TABLES = LEGACY_TABLES | {"workflow_heads", "recoveries"}
MARKER = "workflow_schema_backup"


def _identity(path):
    info = private_path(path).stat()
    return [info.st_dev, info.st_ino]


def _paths(path):
    return Path(str(path) + ".schema1-before.sqlite"), Path(str(path) + ".schema1-before.json")


def _legacy(conn):
    objects = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
    if {row[1] for row in objects if row[0] == "table"} != LEGACY_TABLES or any(
        row[0] != "table" and not (row[0] == "index" and row[2] is None) for row in objects
    ):
        raise SchemaError("Workflow migration requires the owned schema without custom objects")
    meta = conn.execute("SELECT version,store_id FROM workflow_meta").fetchall()
    if len(meta) != 1 or meta[0][0] != 1:
        raise SchemaError("An original schema-1 before-image is required")
    return meta[0][1]


def _read_origin(path, *, store_id=None):
    backup, receipt = _paths(path)
    origin = read_private(receipt)
    if (
        not isinstance(origin, dict)
        or set(origin)
        != {"version", "source", "source_identity", "store_id", "backup_identity", "sha256"}
        or origin["version"] != 1
        or origin["source"] != str(path)
        or origin["source_identity"] != _identity(path)
        or origin["backup_identity"] != _identity(backup)
        or (store_id is not None and origin["store_id"] != store_id)
        or hashlib.sha256(backup.read_bytes()).hexdigest() != origin["sha256"]
    ):
        raise AccessDenied("Workflow before-image identity or content changed")
    return backup, origin


def preserve_schema1(conn, path, identity, store_id):
    """Persist and verify the original committed image before the first schema edit."""
    probe_connection(conn, StoreBinding(path, *identity, "workflow_meta", 1, store_id))
    if _legacy(conn) != store_id:
        raise AccessDenied("Workflow migration store identity changed")
    data = conn.serialize()
    expected = hashlib.sha256(data).hexdigest()
    backup, receipt = _paths(path)
    if not backup.exists() and not receipt.exists():
        # Exclusive creation preserves previous and incomplete attempts. A crash
        # here leaves schema 1 intact; incomplete evidence refuses later migration.
        with backup.open("xb") as stream:
            backup.chmod(0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        origin = {
            "version": 1,
            "source": str(path),
            "source_identity": list(identity),
            "store_id": store_id,
            "backup_identity": _identity(backup),
            "sha256": expected,
        }
        write_private(receipt, origin)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _, origin = _read_origin(path, store_id=store_id)
    if origin["sha256"] != expected:
        raise AccessDenied("Existing before-image differs from the current schema-1 store")
    return origin


def _triggers():
    for table in sorted(CURRENT_TABLES):
        for event in ("INSERT", "UPDATE", "DELETE"):
            name = f"workflow_migration_{table}_{event.lower()}"
            yield (
                name,
                (
                    f"CREATE TRIGGER {name} AFTER {event} ON {table} "
                    f"BEGIN UPDATE {MARKER} SET dirty=1; END"
                ),
            )


def seal_migration(conn, origin):
    """Commit the rollback fence with the schema; later writes invalidate it."""
    conn.execute(
        f"CREATE TABLE {MARKER}(origin TEXT,schema_digest TEXT,"
        "schema_version INTEGER,dirty INTEGER)"
    )
    for _, statement in _triggers():
        conn.execute(statement)
    conn.execute(
        f"INSERT INTO {MARKER} VALUES(?,?,?,0)",
        (
            canonical(origin),
            _schema_digest(conn),
            conn.execute("PRAGMA schema_version").fetchone()[0],
        ),
    )


def _schema_digest(conn):
    return fingerprint(
        [
            tuple(row)
            for row in conn.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"
            )
        ]
    )


def _rows(conn, table):
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def rollback_schema1(path):
    """Trusted code-only rollback; never replace a database or discard later writes."""
    path = private_path(path)
    if path.parent.stat().st_mode & 0o077:
        raise AccessDenied("Workflow storage requires a private directory")
    backup, origin = _read_origin(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=2)
    old = None
    try:
        old = sqlite3.connect(
            backup.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=2
        )
        conn.execute("BEGIN IMMEDIATE")
        old.execute("BEGIN")
        _read_origin(path, store_id=origin["store_id"])
        if _legacy(old) != origin["store_id"] or old.execute("PRAGMA quick_check").fetchall() != [
            ("ok",)
        ]:
            raise AccessDenied("Workflow before-image is not the original valid store")
        probe_connection(
            conn,
            StoreBinding(path, *origin["source_identity"], "workflow_meta", 2, origin["store_id"]),
        )
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables != CURRENT_TABLES | {MARKER}:
            raise AccessDenied("Workflow migration recovery record is unavailable")
        markers = conn.execute(
            f"SELECT origin,schema_digest,schema_version,dirty FROM {MARKER}"
        ).fetchall()
        triggers = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        expected_marker = (
            canonical(origin),
            _schema_digest(conn),
            conn.execute("PRAGMA schema_version").fetchone()[0],
            0,
        )
        if markers != [expected_marker] or triggers != dict(_triggers()):
            raise AccessDenied("Workflow schema has later writes or changed migration guards")
        for pragma in ("user_version", "application_id"):
            if (
                conn.execute(f"PRAGMA {pragma}").fetchone()
                != old.execute(f"PRAGMA {pragma}").fetchone()
            ):
                raise AccessDenied("Workflow metadata differs from the before-image")
        for table in ("runs", "steps", "lineage"):
            if _rows(conn, table) != _rows(old, table):
                raise AccessDenied("Workflow history differs from the before-image")
        heads = conn.execute(
            "SELECT scope,task_id,run_id FROM workflow_heads ORDER BY rowid"
        ).fetchall()
        expected = old.execute("SELECT scope,task_id,id FROM runs ORDER BY rowid").fetchall()
        if heads != expected or conn.execute("SELECT 1 FROM recoveries").fetchone():
            raise AccessDenied("Workflow successor history prevents schema rollback")
        # In-place transactional restoration retains the original database inode.
        # Only runs changed shape; all other historical tables remain untouched.
        for name, _ in _triggers():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE workflow_heads")
        conn.execute("DROP TABLE recoveries")
        conn.execute(f"DROP TABLE {MARKER}")
        conn.execute("DROP TABLE runs")
        conn.execute(old.execute("SELECT sql FROM sqlite_master WHERE name='runs'").fetchone()[0])
        conn.executemany("INSERT INTO runs VALUES(?,?,?,?,?,?)", _rows(old, "runs"))
        conn.execute("UPDATE workflow_meta SET version=1")
        probe_connection(
            conn,
            StoreBinding(path, *origin["source_identity"], "workflow_meta", 1, origin["store_id"]),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if old is not None:
            old.close()
        conn.close()
    return backup
