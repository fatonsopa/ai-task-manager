"""Third-schema connection and immutable workflow-history controls."""

import shutil
import sqlite3

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.coherence import StoreBinding, probe_connection
from ai_task_manager.storage.workflows import WorkflowStore


def test_WD16_third_schema_probe_rollback_retains_identity(tmp_path):
    tmp_path.chmod(0o700)
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    with store.transaction() as conn:
        before = {
            r[0]: conn.execute("SELECT * FROM " + r[0]).fetchall()
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    identity = store.identity
    with store.transaction() as conn:
        probe_connection(
            conn, StoreBinding(store.path, *store.identity, "workflow_meta", 1, store.store_id)
        )
        assert conn.execute("SELECT store_id FROM workflow_meta").fetchone()[0] == store.store_id
    with store.transaction() as conn:
        after = {
            r[0]: conn.execute("SELECT * FROM " + r[0]).fetchall()
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {k: [tuple(v) for v in rows] for k, rows in before.items()} == {
        k: [tuple(v) for v in rows] for k, rows in after.items()
    }
    assert store.identity == identity


def test_WD16_third_schema_copied_connection_refuses(tmp_path):
    tmp_path.chmod(0o700)
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    original = store.path.read_bytes()
    copy, saved, opened = [tmp_path / n for n in ("copy.sqlite", "saved.sqlite", "opened.sqlite")]
    shutil.copyfile(store.path, copy)
    copy.chmod(0o600)
    store.path.rename(saved)
    copy.rename(store.path)
    conn = sqlite3.connect(store.path, isolation_level=None, timeout=0)
    store.path.rename(opened)
    saved.rename(store.path)
    try:
        conn.execute("BEGIN")
        with pytest.raises(AccessDenied):
            probe_connection(
                conn, StoreBinding(store.path, *store.identity, "workflow_meta", 1, store.store_id)
            )
    finally:
        conn.rollback()
        conn.close()
    assert store.path.read_bytes() == opened.read_bytes() == original


def test_WD16_third_schema_precommit_replacement_rolls_back(tmp_path):
    tmp_path.chmod(0o700)
    store = WorkflowStore(tmp_path / "workflow.sqlite")
    original = store.path.read_bytes()
    copy, saved, opened = [tmp_path / n for n in ("copy.sqlite", "saved.sqlite", "opened.sqlite")]
    shutil.copyfile(store.path, copy)
    copy.chmod(0o600)
    try:
        with pytest.raises(AccessDenied):
            with store.transaction() as conn:
                conn.execute("INSERT INTO lineage VALUES('must rollback','task','attempt','{}')")
                store.path.rename(saved)
                copy.rename(store.path)
    finally:
        store.path.rename(opened)
        saved.rename(store.path)
    assert store.path.read_bytes() == opened.read_bytes() == original
