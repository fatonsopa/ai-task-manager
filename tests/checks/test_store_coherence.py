"""Owned replacements are refused; these tests do not promise live-replacement support."""

import json
import shutil
import sqlite3

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.checks import CheckStore
from tests.checks.fixtures import SCOPE, plan, private_dir, sha


def setup_store(tmp_path):
    root = private_dir(tmp_path)
    store = CheckStore(root / "checks.sqlite")
    ref = store.create(plan(), "initial")
    copy = root / "copy.sqlite"
    shutil.copyfile(store.path, copy)
    copy.chmod(0o600)
    return store, ref, copy


def test_copied_image_connection_then_original_path_restored(tmp_path, monkeypatch):
    store, ref, copy = setup_store(tmp_path)
    original = store.path.read_bytes()
    copied = copy.read_bytes()
    saved, opened = tmp_path / "original.sqlite", tmp_path / "opened-copy.sqlite"
    connect = sqlite3.connect
    observations = []

    def substituted(path, *args, **kwargs):
        store.path.rename(saved)
        copy.rename(store.path)
        conn = connect(path, *args, **kwargs)
        observations.append(
            {"opened_inode": store.path.stat().st_ino, "expected_inode": saved.stat().st_ino}
        )
        store.path.rename(opened)
        saved.rename(store.path)
        return conn

    monkeypatch.setattr(sqlite3, "connect", substituted)
    try:
        with pytest.raises(AccessDenied):
            store.read(ref, SCOPE)
    finally:
        observations.append(
            {
                "original_preserved": store.path.read_bytes() == original,
                "copy_preserved": opened.read_bytes() == copied,
            }
        )
        (tmp_path / "coherence-observation.json").write_text(json.dumps(observations))
    assert store.path.read_bytes() == original
    assert opened.read_bytes() == copied


def test_replacement_after_yield_before_commit(tmp_path):
    store, ref, copy = setup_store(tmp_path)
    original = store.path.read_bytes()
    copied = copy.read_bytes()
    saved, opened = tmp_path / "original.sqlite", tmp_path / "replacement.sqlite"
    try:
        with pytest.raises(AccessDenied):
            with store._transaction() as conn:
                conn.execute(
                    "INSERT INTO check_events(plan_ref,kind,body) VALUES(?,?,?)",
                    (ref, "fixture_change", "{}"),
                )
                store.path.rename(saved)
                copy.rename(store.path)
    finally:
        if saved.exists():
            store.path.rename(opened)
            saved.rename(store.path)
        (tmp_path / "coherence-observation.json").write_text(
            json.dumps(
                {
                    "original_preserved": store.path.read_bytes() == original,
                    "copy_preserved": opened.read_bytes() == copied,
                    "original_sha": sha(store.path.read_bytes()),
                    "expected_sha": sha(original),
                }
            )
        )
    assert store.path.read_bytes() == original
    assert opened.read_bytes() == copied
    assert len(store.read(ref, SCOPE)["events"]) == 1


def test_read_validation_preserves_logical_history_and_identity(tmp_path):
    store, ref, _ = setup_store(tmp_path)
    before = store.read(ref, SCOPE)
    expected = store.store_id
    assert store.read(ref, SCOPE) == before
    assert store.store_id == expected
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT store_id FROM check_meta").fetchone() == (expected,)


def test_read_contention_is_prompt_retryable_and_recovers(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from ai_task_manager.storage.checks import CheckStoreBusy

    store, ref, _ = setup_store(tmp_path)
    before = store.read(ref, SCOPE)
    expected = store.store_id
    started = time.monotonic()
    with store._transaction() as conn:
        conn.execute("UPDATE check_meta SET version=version")
        with ThreadPoolExecutor(max_workers=1) as pool:
            error = pool.submit(store.read, ref, SCOPE).exception(timeout=1)
            assert type(error) is CheckStoreBusy and error.retryable
            assert not isinstance(error, AccessDenied)
    assert time.monotonic() - started < 1
    assert store.read(ref, SCOPE) == before and store.store_id == expected


def test_actual_wal_mode_is_not_implicitly_supported(tmp_path):
    store, ref, _ = setup_store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    with pytest.raises(AccessDenied, match="DELETE"):
        store.read(ref, SCOPE)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    assert len(store.read(ref, SCOPE)["events"]) == 1
