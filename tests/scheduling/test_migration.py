import json
import os
import shutil
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.config.claim_store import ClaimStore, enroll_claim_store, verify_claim_store
from ai_task_manager.domains.work.errors import SchemaError, WorkError
from ai_task_manager.domains.work.models import Attempt, AttemptState as A, IssueStatus as S, Scope, SyncState, Task
from ai_task_manager.storage import attempts, events, sessions, work
from ai_task_manager.storage.database import Database, encode

from .helpers import LegacyMigrationFixture, legacy_database_type, legacy_execution


def legacy_store(path, state=A.DONE, resources=False):
    database = legacy_database_type()(path)
    scope = Scope("linear", "workspace", "principal", "project", "session")
    with database.transaction() as conn:
        handle = sessions.register(conn, scope)
        sid, _ = sessions.resolve(conn, handle)
        task = Task("task", sid, "Fixture history", "Retain the authored records.",
                    "a" * 64, "exact-v1", "authored-approval", True, ("complete",),
                    issue_id="issue", observed_status=S.IN_PROGRESS, desired_status=S.IN_PROGRESS,
                    sync_state=SyncState.CONFIRMED, execution_state=state, queued_once=True)
        work.insert(conn, task)
        attempt = Attempt("attempt", task.id, sid, "host", state, 0, worker_id="authored-worker")
        attempts.insert(conn, attempt)
        events.append(conn, sid, task.id, "retained_history", {"original": True})
        conn.execute("INSERT INTO operations VALUES(?,?,?,?)", ("retained-op", sid, "digest", "{}"))
        conn.execute("INSERT INTO checkpoints VALUES(?,?,?,?)", ("checkpoint", attempt.id, "fixture", "{}"))
        if resources:
            conn.execute("INSERT INTO resource_leases VALUES(?,?,?)", ("database", "owned-unsettled", attempt.id))
    return database, handle, task, attempt


def rows(database):
    conn = sqlite3.connect(database.path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                for name in names if not name.startswith("scheduling_") and name not in {"metadata", "publication_fences"}}
    finally:
        conn.close()


@pytest.mark.parametrize("variant", ["identity", "all_rows", "active_unknown", "queued_history", "unsettled_resources", "schema1_active_refusal", "schema2_active_refusal", "schema1_resources_refusal", "schema2_resources_refusal"])
def test_preserve_enrolled_schema3_store(variant, tmp_path):
    if variant.startswith("schema"):
        version = int(variant[6])
        fixture = LegacyMigrationFixture(tmp_path / "core.sqlite", version)
        task = fixture.task(confirmed=False)
        state = A.UNKNOWN if "active" in variant else A.DONE
        attempt = Attempt("old-attempt", task.id, task.session_id, "host", state, 0,
                          worker_id="authored-old-worker")
        with fixture.db.transaction() as conn:
            attempts.insert(conn, attempt)
            if "resources" in variant:
                conn.execute("INSERT INTO resource_leases VALUES(?,?,?)", ("database", "old-owned-resource", attempt.id))
        before = complete_old_state(fixture.db.path)
        with pytest.raises(SchemaError):
            Database(fixture.db.path)
        assert complete_old_state(fixture.db.path) == before
        return
    old, handle, task, attempt = legacy_store(tmp_path / "core.sqlite", A.UNKNOWN if variant == "active_unknown" else A.DONE,
                                            resources=variant == "unsettled_resources")
    before = rows(old)
    identity = before["store_identity"]
    if variant in {"active_unknown", "unsettled_resources"}:
        with pytest.raises(SchemaError):
            Database(old.path)
        assert rows(old) == before
        with old.transaction() as conn:
            assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 3
            assert conn.execute("SELECT active FROM attempts").fetchone()[0] == int(variant == "active_unknown")
            if variant == "unsettled_resources":
                assert conn.execute("SELECT count(*) FROM resource_leases").fetchone()[0] == 1
        return
    if variant == "queued_history":
        with old.transaction() as conn:
            events.append(conn, task.session_id, task.id, "existing_todo_queue_confirmed", {"decision_ref": "retained"})
        before = rows(old)
    new = Database(old.path)
    assert rows(new) == before
    assert rows(new)["store_identity"] == identity
    with new.transaction() as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM scheduling_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM publication_fences").fetchone()[0] == 0
        # Missing old metadata is never silently converted to an empty declaration.
        assert conn.execute("SELECT count(*) FROM scheduling_history").fetchone()[0] == 0


@pytest.mark.parametrize("variant", ["old_open_connection", "old_Database_instance", "old_reopen", "raw_unregistered_connection", "held_old_read_lease"])
def test_old_writer_fence(variant, tmp_path):
    state = A.RUNNING if variant in {"old_Database_instance", "held_old_read_lease"} else A.DONE
    old, handle, task, attempt = legacy_store(tmp_path / "core.sqlite", state)
    cached = legacy_execution(SimpleNamespace(db=old))
    if variant == "held_old_read_lease":
        before = rows(old)
        with cached.work_boundary(handle, attempt.id, attempt.version) as lease:
            assert lease.id == attempt.id
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                Database(old.path)
        assert rows(old) == before
        with old.transaction() as conn:
            assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 3
        return
    if variant == "old_Database_instance":
        assert cached.validate_work_lease(handle, attempt.id, attempt.version).id == attempt.id
        before_refusal = rows(old)
        with pytest.raises(SchemaError):
            Database(old.path)
        assert rows(old) == before_refusal
        # This fixture never starts a process; record the authored terminal fact.
        with old.transaction() as conn:
            attempts.save(conn, replace(attempt, state=A.DONE, version=2))
        with pytest.raises(WorkError):
            cached.validate_work_lease(handle, attempt.id, 2)
    connection = old.connect() if variant == "old_open_connection" else None
    if connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO operations VALUES(?,?,?,?)", ("warm", task.session_id, "d", "{}"))
        connection.rollback()
    before = rows(old)
    new = Database(old.path)
    if variant == "old_reopen":
        with pytest.raises(SchemaError):
            legacy_database_type()(old.path)
    elif variant == "old_Database_instance":
        with pytest.raises(WorkError):
            cached.validate_work_lease(handle, attempt.id, 2)
        with pytest.raises(sqlite3.DatabaseError):
            with old.transaction() as conn:
                conn.execute("INSERT INTO operations VALUES(?,?,?,?)", ("old", task.session_id, "d", "{}"))
    else:
        connection = connection or sqlite3.connect(old.path)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute("INSERT INTO operations VALUES(?,?,?,?)", ("old", task.session_id, "d", "{}"))
            connection.rollback()
        finally:
            connection.close()
    assert rows(new) == before


@pytest.mark.parametrize("variant", ["interrupted_ddl", "rollback", "unknown_future_version", "malformed_tables", "schema1_late_upgrade_rollback", "schema2_late_upgrade_rollback"])
def test_failure_and_future_schema(variant, tmp_path, monkeypatch):
    if variant.startswith("schema"):
        from ai_task_manager.storage import scheduling_schema
        fixture = LegacyMigrationFixture(tmp_path / "core.sqlite", int(variant[6]))
        fixture.task(confirmed=False)
        before = complete_old_state(fixture.db.path)
        def refuse(conn):
            assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 4
            assert conn.execute("SELECT id FROM store_identity").fetchone()
            raise RuntimeError("Authored late upgrade failure")
        monkeypatch.setattr(scheduling_schema, "validate", refuse)
        with pytest.raises(RuntimeError, match="late upgrade"):
            Database(fixture.db.path)
        assert complete_old_state(fixture.db.path) == before
        return
    old, _, _, _ = legacy_store(tmp_path / "core.sqlite")
    before = rows(old)
    if variant in {"interrupted_ddl", "rollback"}:
        from ai_task_manager.storage import scheduling_schema
        if variant == "interrupted_ddl":
            altered = dict(scheduling_schema.TABLES)
            altered["scheduling_history"] = "invalid SQL deliberately ("
            monkeypatch.setattr(scheduling_schema, "TABLES", altered)
        else:
            def refuse(conn):
                raise RuntimeError("authored interruption after migration writes")
            monkeypatch.setattr(scheduling_schema, "validate", refuse)
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            Database(old.path)
        assert rows(old) == before
        with old.transaction() as conn:
            assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 3
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='scheduling_snapshots'").fetchone()
    elif variant == "unknown_future_version":
        with old.transaction() as conn:
            conn.execute("UPDATE metadata SET version=999")
        with pytest.raises(SchemaError):
            Database(old.path)
        assert rows(old) == before
    else:
        new = Database(old.path)
        with new.transaction() as conn:
            conn.execute("DROP TABLE scheduling_wakes")
            conn.execute("CREATE TABLE scheduling_wakes(task_id TEXT, source_event TEXT, reason INTEGER)")
        with pytest.raises(SchemaError):
            Database(old.path)


@pytest.mark.parametrize("variant", ["guard_before_after", "delete_journal", "same_file_identity", "copied_replacement"])
def test_enrollment_guard_compatibility(variant, tmp_path):
    old, _, _, _ = legacy_store(tmp_path / "core.sqlite")
    os.chmod(old.path, 0o600)
    journal = AuthorityJournal(tmp_path / "authority.sqlite", tmp_path / "authority.key")
    enroll_claim_store(old, journal)
    guard = verify_claim_store(old, journal)
    before = journal.claim_store()
    if variant == "delete_journal":
        conn = sqlite3.connect(old.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        with pytest.raises(WorkError):
            Database(old.path, guard=guard.check)
        assert journal.claim_store() == before
        return
    calls = []
    def checked(conn):
        calls.append(id(conn))
        guard.check(conn)
    new = Database(old.path, guard=checked)
    assert journal.claim_store() == before
    with new.transaction() as conn:
        assert new.identity(conn) == before["store_id"]
    if variant == "copied_replacement":
        copied = tmp_path / "copied.sqlite"
        shutil.copyfile(old.path, copied)
        os.chmod(copied, 0o600)
        with pytest.raises(WorkError):
            ClaimStore(str(copied), journal, before).check()
    elif variant == "same_file_identity":
        current = os.stat(new.path)
        assert {"device": current.st_dev, "inode": current.st_ino} == before["file_identity"]
    else:
        assert len(calls) >= 4


def complete_old_state(path):
    """Compare schema and all old rows, including metadata and identity when present."""
    with sqlite3.connect(path) as conn:
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return schema, {name: conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                        for name in names}
