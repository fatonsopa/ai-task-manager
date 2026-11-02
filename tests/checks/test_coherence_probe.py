"""The same connection probe rejects owned substitutions in both admitted schemas."""

import shutil
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.storage.coherence import ConnectionBusy, StoreBinding, probe_connection


@pytest.fixture(params=["check_meta", "merge_meta"])
def owned(tmp_path, request):
    tmp_path.chmod(0o700)
    root = tmp_path
    path = root / "store.sqlite"
    table = request.param
    identity = str(uuid4())
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"CREATE TABLE {table}(version INTEGER,store_id TEXT)")
    conn.execute(f"INSERT INTO {table} VALUES(2,?)", (identity,))
    conn.execute("CREATE TABLE history(value TEXT)")
    conn.execute("INSERT INTO history VALUES('original')")
    conn.close()
    path.chmod(0o600)
    info = path.stat()
    return StoreBinding(path, info.st_dev, info.st_ino, table, 2, identity)


def connection(binding):
    conn = sqlite3.connect(binding.path, isolation_level=None, timeout=0)
    conn.execute("BEGIN")
    return conn


def test_probe_restores_identity_without_committing(owned):
    conn = connection(owned)
    try:
        probe_connection(conn, owned)
        assert conn.in_transaction
        assert conn.execute(f"SELECT version,store_id FROM {owned.table}").fetchall() == [
            (2, owned.store_id)
        ]
        assert conn.execute("SELECT value FROM history").fetchall() == [("original",)]
    finally:
        conn.rollback()
        conn.close()


def test_probe_copied_image_then_expected_path_restored(owned):
    path = owned.path
    root = path.parent
    copy = root / "copy.sqlite"
    saved = root / "saved.sqlite"
    opened = root / "opened.sqlite"
    original = path.read_bytes()
    shutil.copyfile(path, copy)
    copy.chmod(0o600)
    path.rename(saved)
    copy.rename(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=0)
    path.rename(opened)
    saved.rename(path)
    try:
        conn.execute("BEGIN")
        with pytest.raises(AccessDenied):
            probe_connection(conn, owned)
    finally:
        conn.rollback()
        conn.close()
    assert path.read_bytes() == original and opened.read_bytes() == original


def test_probe_rejects_precommit_replacement_and_rolls_back(owned):
    path = owned.path
    root = path.parent
    copy = root / "copy.sqlite"
    saved = root / "saved.sqlite"
    opened = root / "opened.sqlite"
    original = path.read_bytes()
    shutil.copyfile(path, copy)
    copy.chmod(0o600)
    conn = connection(owned)
    try:
        probe_connection(conn, owned)
        conn.execute("INSERT INTO history VALUES('must rollback')")
        path.rename(saved)
        copy.rename(path)
        with pytest.raises(AccessDenied):
            probe_connection(conn, owned)
    finally:
        conn.rollback()
        conn.close()
        path.rename(opened)
        saved.rename(path)
    assert path.read_bytes() == original and opened.read_bytes() == original


@pytest.mark.parametrize("field", ["device", "inode", "store_id", "version", "path"])
def test_probe_enrollment_mismatch_refuses(owned, field):
    values = {
        "device": owned.device + 1,
        "inode": owned.inode + 1,
        "store_id": str(uuid4()),
        "version": owned.version + 1,
        "path": owned.path.parent / "other.sqlite",
    }
    if field == "path":
        shutil.copyfile(owned.path, values["path"])
        values["path"].chmod(0o600)
    conn = connection(owned)
    try:
        with pytest.raises(AccessDenied):
            probe_connection(conn, replace(owned, **{field: values[field]}))
    finally:
        conn.rollback()
        conn.close()


def test_probe_busy_is_infrastructure_and_no_transaction_policy(owned):
    writer = sqlite3.connect(owned.path, isolation_level=None, timeout=0)
    writer.execute("BEGIN IMMEDIATE")
    reader = connection(owned)
    try:
        with pytest.raises(ConnectionBusy) as raised:
            probe_connection(reader, owned)
        assert raised.value.retryable and not isinstance(raised.value, AccessDenied)
        assert reader.in_transaction and writer.in_transaction
    finally:
        reader.rollback()
        reader.close()
        writer.rollback()
        writer.close()
    conn = connection(owned)
    try:
        probe_connection(conn, owned)
    finally:
        conn.rollback()
        conn.close()


def test_probe_rejects_actual_unsupported_journal(owned):
    conn = sqlite3.connect(owned.path, isolation_level=None)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("BEGIN")
        with pytest.raises(AccessDenied, match="DELETE"):
            probe_connection(conn, owned)
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("table", "anything;DROP TABLE history"),
        ("device", True),
        ("inode", -1),
        ("version", True),
        ("store_id", "invalid"),
    ],
)
def test_binding_is_closed_typed_configuration(tmp_path, field, value):
    base = dict(
        path=tmp_path / "file",
        device=1,
        inode=1,
        table="check_meta",
        version=2,
        store_id=str(uuid4()),
    )
    base[field] = value
    with pytest.raises(ValidationError):
        StoreBinding(**base)
