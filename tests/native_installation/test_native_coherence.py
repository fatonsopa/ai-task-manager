"""Native accounting reuses the shared DELETE-journal coherence contract."""

import shutil
import sqlite3

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native.store import NativeStore
from ai_task_manager.storage.coherence import probe_connection


def test_N218_native_probe_copied_connection_then_path_restore(tmp_path):
    tmp_path.chmod(0o700)
    store = NativeStore.create(tmp_path / "store.sqlite")
    path = store.path
    copy, saved, opened = [tmp_path / p for p in ["copy.sqlite", "saved.sqlite", "opened.sqlite"]]
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
            probe_connection(conn, store.binding)
    finally:
        conn.rollback()
        conn.close()
    assert path.read_bytes() == original and opened.read_bytes() == original


def test_N218_native_store_precommit_replacement_rolls_back(tmp_path):
    tmp_path.chmod(0o700)
    store = NativeStore.create(tmp_path / "store.sqlite")
    copy, saved, opened = [tmp_path / p for p in ["copy.sqlite", "saved.sqlite", "opened.sqlite"]]
    original = store.path.read_bytes()
    shutil.copyfile(store.path, copy)
    copy.chmod(0o600)
    try:
        with pytest.raises(AccessDenied):
            with store.transaction() as conn:
                store.event(conn, "must-rollback", None, {})
                store.path.rename(saved)
                copy.rename(store.path)
    finally:
        store.path.rename(opened)
        saved.rename(store.path)
    assert store.path.read_bytes() == original and opened.read_bytes() == original
