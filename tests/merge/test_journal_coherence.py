"""Actual SQLite connection/path controls; no source-application or provider fixtures."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from unit_fixtures import Harness

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.merging.journal import Journal


@pytest.fixture
def h(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return Harness(root / "merge.sqlite")


def test_restored_path_cannot_admit_a_connection_to_its_copied_image(h, monkeypatch):
    path = h.journal.path
    original = path.with_name("original.sqlite")
    copied = path.with_name("copied.sqlite")
    detached = path.with_name("detached.sqlite")
    copied.write_bytes(path.read_bytes())
    copied.chmod(0o600)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    real_connect = sqlite3.connect
    observation = {"original_inode": path.stat().st_ino, "connection_to_copy_opened": False}

    def open_copy_then_restore(selected, *args, **kwargs):
        assert Path(selected) == path
        path.rename(original)
        copied.rename(path)
        try:
            connection = real_connect(selected, *args, **kwargs)
            observation["opened_inode"] = path.stat().st_ino
            observation["connection_to_copy_opened"] = True
        finally:
            path.rename(detached)
            original.rename(path)
        return connection

    denied, returned = False, False
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", open_copy_then_restore)
            try:
                h.journal.current(h.ref)
                returned = True
            except AccessDenied:
                denied = True
            except Exception as error:
                observation["unclassified_error"] = type(error).__name__
        observation.update(
            denied=denied,
            restored_inode=path.stat().st_ino,
            original_bytes_unchanged=hashlib.sha256(path.read_bytes()).hexdigest() == before,
        )
        if returned:
            # This sentinel is the read-followed-by-effect boundary that SQL-write
            # refusal alone cannot establish. Only an actual return reaches it.
            (path.parent / "wrong-connection-effect").write_text("observed unauthorized effect\n")
        observation["effect_occurred"] = (path.parent / "wrong-connection-effect").exists()
        (path.parent / "restored-path-observation.json").write_text(
            json.dumps(observation, indent=2)
        )
        assert observation["connection_to_copy_opened"]
        assert observation["opened_inode"] != observation["original_inode"]
        assert observation["restored_inode"] == observation["original_inode"]
        assert denied and not observation["effect_occurred"], observation
        assert observation["original_bytes_unchanged"]
    finally:
        assert path.stat().st_ino == observation["original_inode"]


def test_precommit_replacement_rolls_back_outer_domain_write(h):
    path = h.journal.path
    retained = path.with_name("retained-original.sqlite")
    replacement = path.with_name("replacement.sqlite")
    before = h.journal.current(h.ref)
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    denied = False
    try:
        try:
            with h.journal._transaction() as conn:
                conn.execute(
                    "UPDATE merge_operations SET facts=? WHERE ref=?",
                    ('{"must_not_commit":true}', h.ref),
                )
                path.rename(retained)
                replacement.rename(path)
        except AccessDenied:
            denied = True
    finally:
        if retained.exists():
            path.rename(replacement)
            retained.rename(path)
    after = h.journal.current(h.ref)
    (path.parent / "precommit-observation.json").write_text(
        json.dumps(
            {
                "denied": denied,
                "outer_write_rolled_back": after == before,
                "store_id_unchanged": h.journal.store_id == Journal(path).store_id,
            },
            indent=2,
        )
    )
    assert denied and after == before


def test_normal_transaction_restores_uuid_and_keeps_outer_write(h):
    expected = h.journal.store_id
    with h.journal._transaction() as conn:
        conn.execute("UPDATE merge_operations SET facts=? WHERE ref=?", ('{"retained":1}', h.ref))
    assert h.journal.current(h.ref)["facts"] == {"retained": 1}
    assert Journal(h.journal.path).store_id == expected


def test_wal_store_is_refused_without_conversion(h):
    conn = sqlite3.connect(h.journal.path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    with pytest.raises(AccessDenied):
        h.journal.current(h.ref)
    check = sqlite3.connect(h.journal.path)
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    check.close()


def test_begin_contention_is_typed_retryable_without_releasing_history(h):
    # Imported only by this new consumer control; the red ABA case runs on the
    # immutable pre-helper implementation without inventing a substitute guard.
    from ai_task_manager.storage.coherence import ConnectionBusy

    before = h.snapshot()
    holder = sqlite3.connect(h.journal.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ConnectionBusy) as caught:
            h.journal.current(h.ref)
        assert caught.value.retryable is True
    finally:
        holder.rollback()
        holder.close()
    assert h.snapshot() == before
