"""Schema migration and rollback controls using owned local SQLite files only."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ai_task_manager.domains.work.errors import AccessDenied, SchemaError
from ai_task_manager.storage.workflows import WorkflowStore

STORE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def legacy(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "workflow.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE workflow_meta(version INTEGER,store_id TEXT);
            CREATE TABLE runs(id TEXT PRIMARY KEY,scope TEXT,task_id TEXT,operation_key TEXT,
                body TEXT,owner TEXT,UNIQUE(scope,task_id),UNIQUE(scope,operation_key));
            CREATE TABLE steps(run_id TEXT,name TEXT,payload TEXT,result TEXT,
                PRIMARY KEY(run_id,name));
            CREATE TABLE lineage(resource TEXT PRIMARY KEY,task_id TEXT,attempt_id TEXT,body TEXT);
            INSERT INTO runs VALUES
                ('original','scope','task','key','{  "original": true }','owner');
            INSERT INTO steps VALUES('original','start','{  "exact": true }',NULL);
            INSERT INTO lineage VALUES('resource','other-task','prior','{  "exact": true }');
        """)
        conn.execute("INSERT INTO workflow_meta VALUES(1,?)", (STORE_ID,))
    path.chmod(0o600)
    return path


def snapshot(path):
    with sqlite3.connect(path) as conn:
        tables = sorted(
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        )
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def test_owned_before_image_and_in_place_rollback_preserve_original_history(legacy):
    original, raw, inode = snapshot(legacy), legacy.read_bytes(), legacy.stat().st_ino
    store = WorkflowStore(legacy)
    backup = Path(str(legacy) + ".schema1-before.sqlite")
    receipt = json.loads(Path(str(legacy) + ".schema1-before.json").read_text())
    assert backup.read_bytes() == raw
    assert backup.stat().st_mode & 0o777 == 0o600
    assert receipt["source_identity"] == [legacy.stat().st_dev, inode]
    assert snapshot(backup) == original
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert WorkflowStore(legacy).store_id == STORE_ID
    assert WorkflowStore.rollback_schema1(legacy) == backup
    assert snapshot(legacy) == original
    assert legacy.stat().st_ino == inode
    assert backup.read_bytes() == raw
    # A schema-2 handle cannot write after the trusted rollback.
    with pytest.raises(AccessDenied):
        with store.transaction():
            pass


@pytest.mark.parametrize(
    "table,statement",
    [
        ("runs", "UPDATE runs SET owner='later'"),
        ("steps", "UPDATE steps SET result='{}'"),
        ("lineage", "UPDATE lineage SET attempt_id='later'"),
        ("workflow_heads", "DELETE FROM workflow_heads"),
        ("recoveries", "INSERT INTO recoveries VALUES('original','next','scope','new','{}')"),
        ("workflow_meta", "UPDATE workflow_meta SET store_id=store_id"),
    ],
)
def test_any_committed_workflow_write_fences_rollback(legacy, table, statement):
    store = WorkflowStore(legacy)
    with store.transaction() as conn:
        conn.execute(statement)
    before = snapshot(legacy)
    with pytest.raises(AccessDenied, match="later writes"):
        WorkflowStore.rollback_schema1(legacy)
    assert snapshot(legacy) == before


def test_successor_and_writes_reverted_in_a_later_commit_cannot_be_rolled_back(legacy):
    store = WorkflowStore(legacy)
    with store.transaction() as conn:
        conn.execute("INSERT INTO runs VALUES('next','scope','task','new','{}',NULL)")
        conn.execute("UPDATE workflow_heads SET run_id='next'")
        conn.execute("INSERT INTO recoveries VALUES('original','next','scope','new','{}')")
    successor = snapshot(legacy)
    with pytest.raises(AccessDenied):
        WorkflowStore.rollback_schema1(legacy)
    assert snapshot(legacy) == successor
    with store.transaction() as conn:
        conn.execute("DELETE FROM recoveries")
        conn.execute("UPDATE workflow_heads SET run_id='original'")
        conn.execute("DELETE FROM runs WHERE id='next'")
    with pytest.raises(AccessDenied, match="later writes"):
        WorkflowStore.rollback_schema1(legacy)


def test_rolled_back_work_is_not_a_committed_effect(legacy):
    store = WorkflowStore(legacy)
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            conn.execute("UPDATE runs SET owner='aborted'")
            raise RuntimeError("abort transaction")
    WorkflowStore.rollback_schema1(legacy)
    assert snapshot(legacy)["runs"][0][-1] == "owner"


@pytest.mark.parametrize(
    "statement", ["CREATE TABLE workflow_heads", "UPDATE workflow_meta SET version=2"]
)
def test_failed_migration_keeps_verified_backup_and_retries_original_store(
    legacy, monkeypatch, statement
):
    raw = legacy.read_bytes()
    connect = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith(statement):
                raise sqlite3.OperationalError("injected migration failure")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=Failing))
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            WorkflowStore(legacy)
    assert legacy.read_bytes() == raw
    assert Path(str(legacy) + ".schema1-before.sqlite").read_bytes() == raw
    WorkflowStore(legacy)
    WorkflowStore.rollback_schema1(legacy)
    assert snapshot(legacy)["workflow_meta"] == [(1, STORE_ID)]


@pytest.mark.parametrize("stage", ["before_commit", "after_commit", "during_rollback"])
def test_process_death_preserves_recoverable_original_history(legacy, stage):
    original = snapshot(legacy)
    if stage == "during_rollback":
        WorkflowStore(legacy)
    code = r"""
import os,sqlite3,sys
from ai_task_manager.storage.workflows import WorkflowStore
connect=sqlite3.connect
stage=sys.argv[2]
class Crashing(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        if (stage == "before_commit" and sql.startswith("UPDATE workflow_meta SET version=2")) or (
            stage == "during_rollback" and sql == "DROP TABLE runs"):
            os._exit(74)
        return super().execute(sql,*args,**kwargs)
sqlite3.connect=lambda *a,**k: connect(*a,**k,factory=Crashing)
if stage == "during_rollback":
    WorkflowStore.rollback_schema1(sys.argv[1])
else:
    WorkflowStore(sys.argv[1])
os._exit(74)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(legacy), stage],
        env={**os.environ, "PYTHONPATH": str(Path("src").absolute())},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 74, result.stderr
    if stage == "before_commit":
        assert snapshot(legacy) == original
        WorkflowStore(legacy)
    WorkflowStore.rollback_schema1(legacy)
    assert snapshot(legacy) == original


@pytest.mark.parametrize("change", ["backup", "receipt", "file", "trigger", "incomplete"])
def test_unknown_or_changed_recovery_evidence_never_changes_the_store(legacy, change):
    if change != "incomplete":
        WorkflowStore(legacy)
    backup = Path(str(legacy) + ".schema1-before.sqlite")
    if change in {"backup", "incomplete"}:
        backup.write_bytes(b"invalid retained attempt")
        backup.chmod(0o600)
    elif change == "receipt":
        Path(str(legacy) + ".schema1-before.json").unlink()
    elif change == "file":
        replacement = legacy.with_suffix(".replacement")
        replacement.write_bytes(legacy.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(legacy)
    else:
        with sqlite3.connect(legacy) as conn:
            conn.execute("DROP TRIGGER workflow_migration_runs_insert")
    before = legacy.read_bytes()
    with pytest.raises(AccessDenied):
        if change == "incomplete":
            WorkflowStore(legacy)
        else:
            WorkflowStore.rollback_schema1(legacy)
    assert legacy.read_bytes() == before


def test_fresh_schema2_does_not_claim_to_have_a_schema1_before_image(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "new.sqlite"
    WorkflowStore(path)
    assert not Path(str(path) + ".schema1-before.sqlite").exists()
    with pytest.raises(AccessDenied):
        WorkflowStore.rollback_schema1(path)


def test_custom_schema1_objects_refuse_before_migration(legacy):
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE INDEX custom_run_index ON runs(owner)")
    before = legacy.read_bytes()
    with pytest.raises(SchemaError, match="custom objects"):
        WorkflowStore(legacy)
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("change", ["index", "reverted_index", "user_version"])
def test_later_schema_or_metadata_changes_fence_rollback(legacy, change):
    WorkflowStore(legacy)
    with sqlite3.connect(legacy) as conn:
        if change == "user_version":
            conn.execute("PRAGMA user_version=7")
        else:
            conn.execute("CREATE INDEX later_index ON runs(owner)")
            if change == "reverted_index":
                conn.execute("DROP INDEX later_index")
    before = legacy.read_bytes()
    with pytest.raises(AccessDenied):
        WorkflowStore.rollback_schema1(legacy)
    assert legacy.read_bytes() == before


def test_writer_waits_for_rollback_then_refuses_its_old_schema_handle(legacy, monkeypatch):
    store = WorkflowStore(legacy)
    started, finished = threading.Event(), threading.Event()
    failures = []

    def write():
        started.set()
        try:
            with store.transaction() as conn:
                conn.execute("UPDATE runs SET owner='too-late'")
        except AccessDenied as error:
            failures.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=write)
    connect = sqlite3.connect

    class Paused(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "DROP TABLE runs":
                thread.start()
                assert started.wait(1)
                assert not finished.wait(0.05)
            return super().execute(sql, *args, **kwargs)

    try:
        with monkeypatch.context() as m:
            m.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=Paused))
            WorkflowStore.rollback_schema1(legacy)
    finally:
        if thread.ident is not None:
            thread.join(timeout=3)
    assert not thread.is_alive() and len(failures) == 1
    assert snapshot(legacy)["runs"][0][-1] == "owner"
