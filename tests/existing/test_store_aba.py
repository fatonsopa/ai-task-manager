"""Actual SQLite refusal before read-only authority; no descriptor-attestation claim."""

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.config import claim_store
from ai_task_manager.config.claim_store import (
    enroll_claim_store,
    open_claim_store,
    verify_claim_store,
)
from ai_task_manager.domains.work.errors import AccessDenied, InvalidTransition
from tests.domains.helpers import Rig


@pytest.fixture
def store(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    task, attempt = rig.running()
    os.chmod(rig.db.path, 0o600)
    journal = AuthorityJournal(tmp_path / "authority.sqlite", tmp_path / "authority.key")
    enroll_claim_store(rig.db, journal)
    guard = verify_claim_store(rig.db, journal)
    return SimpleNamespace(
        rig=rig,
        task=task,
        attempt=attempt,
        journal=journal,
        guard=guard,
        path=Path(rig.db.path),
        root=tmp_path,
    )


def facts(store):
    with sqlite3.connect(store.path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "store_identity",
                "tasks",
                "attempts",
                "outbox",
                "events",
                "operations",
                "resource_leases",
                "issue_bindings",
                "issue_lifecycle",
            )
        }


def hold(store):
    rig, task = store.rig, store.task
    ref = rig.proof(
        "human_decision", task.id, {"previous_revision": task.decision_revision, "hold": True}
    )
    return rig.core.decisions.record(rig.handle, task.id, task.version, ref, rig.key())


def boundary(store, effect):
    with store.rig.core.execution.work_boundary(
        store.rig.handle, store.attempt.id, store.attempt.version
    ):
        effect.write_text("Owned boundary effect.\n")


@contextmanager
def opened_copy(store, *, restore_at="connect"):
    copy, backup = store.root / "copy.sqlite", store.root / "original-away.sqlite"
    shutil.copy2(store.path, copy)
    original_inode, copied_inode = store.path.stat().st_ino, copy.stat().st_ino
    assert original_inode != copied_inode
    hold(store)
    assert not store.task.human_hold
    real_connect, real_guard = store.rig.db.connect, store.rig.db.guard
    observations = []

    def restore():
        os.rename(store.path, copy)
        os.rename(backup, store.path)

    def connect():
        os.rename(store.path, backup)
        os.rename(copy, store.path)
        conn = real_connect()
        # The connection actually reads the copy's pre-hold task and shared UUID.
        observations.append(
            conn.execute("SELECT data FROM tasks WHERE id=?", (store.task.id,)).fetchone()[0]
        )
        assert store.rig.db.identity(conn) == store.guard.context["store_id"]
        if restore_at == "connect":
            restore()
        return conn

    def guard(conn):
        if restore_at == "guard" and backup.exists():
            restore()
        real_guard(conn)

    store.rig.db.connect, store.rig.db.guard = connect, guard
    try:
        yield observations
    finally:
        store.rig.db.connect, store.rig.db.guard = real_connect, real_guard
        if backup.exists():
            restore()
        assert store.path.stat().st_ino == original_inode


@pytest.mark.parametrize("when", ["connect", "guard"])
def test_ea84_actual_copy_aba_refuses_before_readonly_work_boundary(store, when):
    import json

    effect = store.root / "must-not-publish.txt"
    with opened_copy(store, restore_at=when) as observations:
        before = facts(store)
        with pytest.raises(AccessDenied) as caught:
            boundary(store, effect)
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert caught.value.__cause__.sqlite_errorname == "SQLITE_READONLY_DBMOVED"
        assert observations and not json.loads(observations[0])["human_hold"]
        assert not effect.exists() and facts(store) == before
    assert store.rig.current(store.task).human_hold


def test_ea84_valid_delete_boundary_and_same_key_mutation_leave_guard_no_domain_effect(store):
    rig = store.rig
    before = facts(store)
    effect = store.root / "valid-effect.txt"
    boundary(store, effect)
    assert effect.exists() and facts(store) == before
    prose = rig.core.get_task(rig.handle, store.task.id)
    from ai_task_manager.domains.work.models import TaskProse

    new_prose = TaskProse("Verify guarded replay", "Preserve exactly one committed task intent.")
    approval, key = rig.approval(new_prose), rig.key()
    submitted = rig.core.intake.submit(rig.handle, new_prose, approval, key)
    committed = facts(store)
    assert len(committed["tasks"]) == len(before["tasks"]) + 1
    assert rig.core.intake.submit(rig.handle, new_prose, approval, key) == submitted
    assert facts(store) == committed
    assert rig.core.get_task(rig.handle, prose.id) == prose


@pytest.mark.parametrize("mode", ["wal", "off", "memory", "persist", "truncate"])
def test_ea84_actual_other_journal_modes_refuse_without_switching_or_effect(store, mode):
    real_connect = store.rig.db.connect
    observed = []

    def connect():
        conn = real_connect()
        observed.append(conn.execute(f"PRAGMA main.journal_mode={mode}").fetchone()[0])
        return conn

    store.rig.db.connect = connect
    before, effect = facts(store), store.root / "must-not-publish.txt"
    with pytest.raises(AccessDenied, match="DELETE journal"):
        boundary(store, effect)
    assert observed == [mode] and not effect.exists() and facts(store) == before
    store.rig.db.connect = real_connect
    if mode == "wal":
        with real_connect() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


class Connection:
    def __init__(self, conn, fault):
        self.conn, self.fault = conn, fault

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, parameters=()):
        return self.fault(self.conn, sql, parameters)


@pytest.mark.parametrize("stage", ["write", "rollback", "release", "identity_recheck"])
def test_ea84_validation_step_failure_refuses_and_rolls_back(store, stage):
    real_connect = store.rig.db.connect
    fired = []

    def fault(conn, sql, parameters):
        matches = {
            "write": sql.startswith("UPDATE main.store_identity"),
            "rollback": sql.startswith("ROLLBACK TO aitm_claim_store"),
            "release": sql.startswith("RELEASE aitm_claim_store"),
            "identity_recheck": sql == "SELECT id FROM store_identity" and bool(fired),
        }
        if stage == "identity_recheck" and sql.startswith("RELEASE aitm_claim_store"):
            fired.append("released")
        if matches[stage]:
            fired.append(stage)
            raise sqlite3.OperationalError("Authored validation step failure")
        return conn.execute(sql, parameters)

    store.rig.db.connect = lambda: Connection(real_connect(), fault)
    before, effect = facts(store), store.root / "must-not-publish.txt"
    with pytest.raises((AccessDenied, sqlite3.OperationalError)):
        boundary(store, effect)
    store.rig.db.connect = real_connect
    assert stage in fired and not effect.exists() and facts(store) == before


@pytest.mark.parametrize("when", ["generator", "after_release", "precommit"])
def test_ea84_namespace_change_at_each_validation_boundary_refuses(store, monkeypatch, when):
    copy, backup = store.root / "replacement.sqlite", store.root / "original-away.sqlite"
    shutil.copy2(store.path, copy)
    original, changed = facts(store), []
    real_connect, generator = store.rig.db.connect, claim_store.uuid4

    def replace():
        if not changed:
            os.rename(store.path, backup)
            os.rename(copy, store.path)
            changed.append(True)

    if when == "generator":

        def generate():
            replace()
            return generator()

        monkeypatch.setattr(claim_store, "uuid4", generate)
    elif when == "after_release":

        def fault(conn, sql, parameters):
            result = conn.execute(sql, parameters)
            if sql.startswith("RELEASE aitm_claim_store"):
                replace()
            return result

        store.rig.db.connect = lambda: Connection(real_connect(), fault)
    effect = store.root / "must-not-publish.txt"
    try:
        with pytest.raises(AccessDenied):
            if when == "precommit":
                with store.rig.db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO issue_lifecycle VALUES('owned-late-change',0,0,'fixture')"
                    )
                    replace()
            else:
                boundary(store, effect)
        assert changed and not effect.exists()
    finally:
        store.rig.db.connect = real_connect
        if backup.exists():
            os.rename(store.path, copy)
            os.rename(backup, store.path)
    assert facts(store) == original


def test_ea84_validation_requires_outer_transaction_and_available_mode(store):
    with store.rig.db.connect() as conn:
        with pytest.raises(AccessDenied, match="owned transaction"):
            store.guard.check(conn)
        conn.execute("BEGIN IMMEDIATE")

        def fault(actual, sql, parameters):
            if sql == "PRAGMA main.journal_mode":
                raise sqlite3.OperationalError("Authored unavailable journal query")
            return actual.execute(sql, parameters)

        with pytest.raises(AccessDenied, match="mode is unavailable"):
            store.guard.check(Connection(conn, fault))
        conn.rollback()


@pytest.mark.parametrize("value", ["equal", "malformed", "failure"])
def test_ea84_generator_cannot_degrade_to_a_noop(store, monkeypatch, value):
    before, effect = facts(store), store.root / "must-not-publish.txt"

    def generate():
        if value == "failure":
            raise RuntimeError("Authored unavailable UUID generator")
        return store.guard.context["store_id"] if value == "equal" else "invalid"

    monkeypatch.setattr(claim_store, "uuid4", generate)
    with pytest.raises(AccessDenied, match="identity is unavailable"):
        boundary(store, effect)
    assert not effect.exists() and facts(store) == before


def test_ea84_conditional_update_must_match_exactly_one_expected_row(store):
    before = facts(store)
    with pytest.raises(AccessDenied, match="uniquely matched"):
        with store.rig.db.transaction() as conn:
            claim_store._validate_transaction(conn, str(uuid4()))
    assert facts(store) == before


@pytest.mark.parametrize("mode", ["readonly_uri", "query_only"])
def test_ea84_actual_readonly_connection_validation_refuses(store, mode):
    conn = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    if mode == "query_only":
        conn.close()
        conn = store.rig.db.connect()
        conn.execute("PRAGMA query_only=ON")
    before = facts(store)
    try:
        conn.execute("BEGIN")
        with pytest.raises(AccessDenied) as caught:
            store.guard.check(conn)
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert caught.value.__cause__.sqlite_errorname == "SQLITE_READONLY"
        conn.rollback()
    finally:
        conn.close()
    assert facts(store) == before


def test_ea84_unknown_mode_query_is_a_failure_injection_not_an_actual_sqlite_mode(store):
    class Mode:
        def __iter__(self):
            return iter([("unrecognized-fixture-mode",)])

    real_connect = store.rig.db.connect
    observed = []

    def fault(conn, sql, parameters):
        observed.append(sql)
        return Mode() if sql == "PRAGMA main.journal_mode" else conn.execute(sql, parameters)

    store.rig.db.connect = lambda: Connection(real_connect(), fault)
    effect, before = store.root / "must-not-publish.txt", facts(store)
    with pytest.raises(AccessDenied, match="DELETE journal"):
        boundary(store, effect)
    assert not any(sql.startswith("UPDATE main.store_identity") for sql in observed)
    assert not effect.exists() and facts(store) == before


def test_ea84_guard_savepoint_preserves_existing_outer_changes_and_allows_outer_rollback(store):
    before = facts(store)
    with pytest.raises(RuntimeError, match="Owned outer abort"):
        with store.rig.db.transaction() as conn:
            conn.execute("INSERT INTO issue_lifecycle VALUES('owned-outer-fact',0,0,'fixture')")
            store.guard.check(conn)
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM issue_lifecycle WHERE identity_key='owned-outer-fact'"
                ).fetchone()[0]
                == 1
            )
            raise RuntimeError("Owned outer abort")
    assert facts(store) == before
    with store.rig.db.transaction() as conn:
        conn.execute("INSERT INTO issue_lifecycle VALUES('owned-outer-fact',0,0,'fixture')")
        store.guard.check(conn)
    after = facts(store)
    assert after.pop("issue_lifecycle") == before.pop("issue_lifecycle") + [
        ("owned-outer-fact", 0, 0, "fixture")
    ]
    assert after == before


@pytest.mark.parametrize("stage", ["temporary_write", "savepoint_rollback", "domain_change"])
def test_ea84_actual_process_crash_restores_identity_and_domain_facts(store, stage):
    import json

    payload = store.root / "crash-input.json"
    payload.write_text(
        json.dumps(
            {
                "database": str(store.path),
                "authority": str(store.journal.path),
                "key": str(store.journal.key_path),
                "handle": store.rig.handle.token,
                "stage": stage,
            }
        )
    )
    os.chmod(payload, 0o600)
    before = facts(store)
    env = {**os.environ, "PYTHONPATH": str(Path.cwd()) + os.pathsep + str(Path.cwd() / "src")}
    result = subprocess.run(
        [sys.executable, "tests/fixtures/existing/store_guard_crash.py", str(payload)],
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 86, (result.stdout, result.stderr)
    recovered, _ = open_claim_store(store.path, store.journal)
    with recovered.transaction() as conn:
        assert recovered.identity(conn) == store.guard.context["store_id"]
    assert facts(store) == before


def test_ea84_hold_wins_before_boundary_no_effect(store):
    hold(store)
    effect, before = store.root / "must-not-publish.txt", facts(store)
    with pytest.raises(InvalidTransition):
        boundary(store, effect)
    assert not effect.exists() and facts(store) == before


def test_ea84_boundary_serializes_a_later_human_hold_over_the_same_store(store):
    started, finished = threading.Event(), threading.Event()
    errors = []

    def decide():
        started.set()
        try:
            hold(store)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    effect = store.root / "authorized-before-hold.txt"
    with store.rig.core.execution.work_boundary(
        store.rig.handle, store.attempt.id, store.attempt.version
    ):
        thread = threading.Thread(target=decide)
        thread.start()
        assert started.wait(2) and not finished.wait(0.1)
        effect.write_text("The lease transaction won before the later hold.\n")
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors and finished.is_set() and effect.exists()
    assert store.rig.current(store.task).human_hold
    with pytest.raises(InvalidTransition):
        boundary(store, store.root / "must-not-publish.txt")
