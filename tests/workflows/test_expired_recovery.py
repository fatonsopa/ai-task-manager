"""ER01–ER10: real local stores/owners; authority/provider observations are authored."""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.linear.receipts import ProviderJournal, encoded
from ai_task_manager.adapters.workflow_control.identity import (
    endpoint_binding,
    file_binding,
    process_start,
    source_digest,
)
from ai_task_manager.adapters.workflow_control.recovery import RecoveryHistory
from ai_task_manager.application.service.models import SCHEMAS
from ai_task_manager.application.workflows.driver import WorkflowDriver
from ai_task_manager.application.workflows.models import WorkflowPlan, WorkflowPlans
from ai_task_manager.application.workflows.recovery import RecoveryAuthorization
from ai_task_manager.application.workflows.service import WorkflowService
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied, IdempotencyConflict, WorkError
from ai_task_manager.domains.work.models import Attempt, AttemptState
from ai_task_manager.storage import attempts, issue_claims, work
from ai_task_manager.storage.workflows import WorkflowStore
from ai_task_manager.supervisor.journal import RuntimeJournal
from tests.domains.helpers import Rig


class RecoveryFixture:
    def __init__(self):
        # Short physical socket paths work on macOS too.
        self.root = Path(tempfile.mkdtemp(prefix="er-", dir="/tmp")).resolve()
        self.root.chmod(0o700)
        self.r = r = Rig(self.root / "core.sqlite")
        Path(r.db.path).chmod(0o600)
        self.task = r.queued(code=False)
        self.runtime = RuntimeJournal(self.root / "runtime.sqlite")
        self.host = HostJournal(self.root / "host.sqlite", self.root / "host.key")
        self.provider = ProviderJournal(self.root / "provider.sqlite")
        self.store = WorkflowStore(self.root / "workflow.sqlite")
        self.config = self.root / "config.json"
        self.sockets = []
        self.old_process = subprocess.Popen(
            [sys.executable, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        self.old_record, self.old_endpoint = self.owner(1, self.old_process.pid)
        self.history = RecoveryHistory(
            self.config,
            core=r.db.path,
            workflow=self.store.path,
            runtime=self.runtime.path,
            host=self.host.path,
            provider=self.provider.path,
        )
        self.old_plan = WorkflowPlan(
            "old-plan", "assignment", "fixture", "base", ("result",), ("result",), r.now + 10
        )
        self.plan = replace(self.old_plan, id="fresh-plan", deadline=r.now + 120)
        self.plans = WorkflowPlans((self.old_plan, self.plan))
        self.port = SimpleNamespace(binding=lambda task, plan: "registered:" + plan.digest)
        self.driver = self.new_driver()
        self.ref = self.driver.begin(self.task.id, self.old_plan.id, "original-key")
        self.authorization = RecoveryAuthorization(
            "trusted-operator-selection",
            r.scope,
            self.ref,
            self.task.id,
            self.task.version,
            self.task.decision_revision,
            self.plan.id,
            self.plan.digest,
            "fresh-key",
            r.now + 150,
        )
        self.decision_expiry = r.now + 150
        self.old_process.stdin.close()
        self.old_process.wait(timeout=5)
        self.sockets[0].close()
        self.old_endpoint.unlink()
        r.now += 20
        self.owner(2, os.getpid())

    def owner(self, number, pid):
        record, endpoint = (self.root / f"owner-{number}.json", self.root / f"owner-{number}.sock")
        instance = str(uuid4())
        write_private(
            self.config,
            {
                "core_database": self.r.db.path,
                "linear": {"journal": self.provider.path},
                "workflow_control": {
                    "record": str(record),
                    "endpoint": str(endpoint),
                    "instance": instance,
                },
            },
            replace=self.config.exists(),
        )
        stream = socket.socket(socket.AF_UNIX)
        stream.bind(str(endpoint))
        endpoint.chmod(0o600)
        self.sockets.append(stream)
        write_private(
            record,
            {
                "version": 1,
                "instance": instance,
                "scope": asdict(self.r.scope),
                "config": file_binding(self.config),
                "source": source_digest(),
                "pid": pid,
                "start": process_start(pid),
                "endpoint": endpoint_binding(endpoint),
                "key": "a" * 64,
            },
        )
        return record, endpoint

    def decision(self, scope, task, target, action):
        return self.r.proof(
            "decision",
            task.id,
            {
                "target": target,
                "revision": task.decision_revision,
                "actor": "agent",
            },
            expires_at=self.decision_expiry,
        )

    def new_driver(self, faults=None):
        return WorkflowDriver(
            self.r.core,
            self.r.handle,
            self.store,
            self.plans,
            runtime=self.port,
            repository=object(),
            provider=object(),
            decisions=self.decision,
            recovery_history=self.history,
            faults=faults,
        )

    def service(self, driver=None):
        return WorkflowService(driver or self.driver, {self.task.id: self.plan.id})

    def recover(self, authorization=None):
        return self.service().recover_expired(authorization or self.authorization)

    def rows(self):
        with self.store.transaction() as conn:
            return {
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in ("runs", "steps", "lineage", "workflow_heads", "recoveries")
            }

    def mutate_task(self, **changes):
        with self.r.db.transaction() as conn:
            old = work.get(conn, self.driver.session_id, self.task.id)
            work.save(conn, replace(old, **changes), old.version)

    def close(self):
        if hasattr(self, "old_process") and self.old_process.poll() is None:
            self.old_process.terminate()
            self.old_process.wait(timeout=5)
        for stream in getattr(self, "sockets", []):
            stream.close()
        shutil.rmtree(self.root)


@pytest.fixture
def f():
    value = object.__new__(RecoveryFixture)
    try:
        value.__init__()
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("start_intent", [False, True])
def test_ER01_success_preserves_original_bytes(f, start_intent):
    if start_intent:
        f.store.claim(f.ref, f.r.scope, "old-controller")
        f.store.intend(f.ref, "old-controller", "start", {"cursor": 4, "decision_ref": "expired"})
        f.store.release(f.ref, "old-controller")
    before = f.rows()
    successor = f.recover()
    after = f.rows()
    assert after["runs"][0] == before["runs"][0]
    assert after["steps"] == before["steps"]
    assert after["lineage"] == before["lineage"]
    assert len(after["runs"]) == 2 and len(after["recoveries"]) == 1
    assert after["workflow_heads"][0][2] == successor
    row = f.store.read(successor, f.r.scope)
    assert row["plan"] == json.loads(json.dumps(asdict(f.plan)))
    assert row["steps"] == {} and row["attempt_id"] is None
    with f.r.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_ER02_original_keys_and_refs_never_rebind(f):
    successor = f.recover()
    assert f.store.find(f.r.scope, f.task.id, "original-key") == f.ref
    assert f.driver.begin(f.task.id, f.old_plan.id, "original-key") == f.ref
    assert f.store.find(f.r.scope, f.task.id, "fresh-key") == successor
    with pytest.raises(IdempotencyConflict):
        f.driver.begin(f.task.id, f.plan.id, "another-key")
    with pytest.raises(AccessDenied, match="history"):
        f.service().resume(f.ref)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expires_at", 1010),
        ("expires_at", 1050),
        ("task_version", 999),
        ("decision_revision", 99),
        ("plan_digest", "different"),
        ("task_id", "foreign"),
        ("operation_key", "original-key"),
        ("scope", None),
    ],
)
def test_ER03_authorization_mismatch_refuses(f, field, value):
    if field == "scope":
        value = replace(f.r.scope, principal="foreign")
    before = f.rows()
    with pytest.raises(WorkError):
        f.recover(replace(f.authorization, **{field: value}))
    assert f.rows() == before


@pytest.mark.parametrize("expiry", [None, 1010, 1050])
def test_ER03_current_authority_must_cover_fresh_plan(f, expiry):
    f.decision_expiry = expiry
    with pytest.raises(WorkError):
        f.recover()
    assert len(f.rows()["runs"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"human_hold": True},
        {"decision_revision": 1},
        {"prose_digest": "revised"},
        {"queued_once": False},
        {"delivered_once": True},
        {"execution_state": "unknown"},
        {"observed_status": "Done"},
        {"observed_status": "In Dev"},
        {"observed_status": "Canceled"},
        {"desired_status": "On Hold"},
        {"sync_state": "uncertain"},
        {"code_bearing": True},
        {"completed_work": ("complete",)},
    ],
)
def test_ER03_changed_task_refuses(f, change):
    f.mutate_task(**change)
    with pytest.raises(WorkError):
        f.recover()
    assert len(f.rows()["runs"]) == 1


@pytest.mark.parametrize("fence", ["delivered", "terminal"])
def test_ER03_canonical_history_overrides_todo(f, fence):
    with f.r.db.transaction() as conn:
        issue_claims.fence(conn, f.r.scope, f.task.issue_id, **{fence: True})
    with pytest.raises(WorkError):
        f.recover()


@pytest.mark.parametrize("state", list(AttemptState))
def test_ER04_any_attempt_refuses_even_if_settled(f, state):
    with f.r.db.transaction() as conn:
        attempts.insert(conn, Attempt("prior", f.task.id, f.driver.session_id, "host", state, 0))
    with pytest.raises(WorkError):
        f.recover()


@pytest.mark.parametrize(
    "table,values",
    [
        ("runtime_attempts", ("orphan", "other", "{}")),
        ("runtime_effects", ("orphan", "resources", "{}")),
        ("runtime_observations", ("orphan", "orphan", "worker", "{}", "digest")),
        ("runtime_receipts", ("orphan", "{}", "mac")),
        ("host_intents", ("orphan", "{}", "mac")),
        ("host_records", ("orphan", "{}", "mac")),
    ],
)
def test_ER04_orphan_runtime_or_native_history_refuses(f, table, values):
    journal = f.runtime if table.startswith("runtime") else f.host
    with journal.transaction() as conn:
        conn.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values)
    with pytest.raises(WorkError):
        f.recover()


@pytest.mark.parametrize("name,result", [("start", {}), ("execute", None), ("verify", {})])
def test_ER04_unknown_or_effected_steps_refuse(f, name, result):
    f.store.claim(f.ref, f.r.scope, "retained")
    f.store.intend(f.ref, "retained", name, {})
    if result is not None:
        f.store.finish(f.ref, "retained", name, result)
    f.store.release(f.ref, "retained")
    before = f.rows()
    with pytest.raises(WorkError):
        f.recover()
    assert f.rows() == before


def test_ER04_unresolved_controller_claim_refuses(f):
    f.store.claim(f.ref, f.r.scope, "unresolved")
    with pytest.raises(WorkError):
        f.recover()
    assert f.store.read(f.ref, f.r.scope)["owner"] == "unresolved"


@pytest.mark.parametrize("state", ["pending", "leased", "uncertain", "unknown"])
def test_ER05_unsettled_core_write_refuses(f, state):
    with f.r.db.transaction() as conn:
        conn.execute("UPDATE outbox SET state=? WHERE task_id=?", (state, f.task.id))
    with pytest.raises(WorkError):
        f.recover()


@pytest.mark.parametrize("state", ["sending", "uncertain", "unknown"])
def test_ER05_unsettled_provider_send_refuses(f, state):
    with f.provider.transaction() as conn:
        conn.execute(
            "INSERT INTO sends VALUES(?,?,?,?,?,NULL,NULL,NULL,NULL)",
            ("send", encoded(asdict(f.r.scope)), "op", 1, state),
        )
    with pytest.raises(WorkError):
        f.recover()


@pytest.mark.parametrize("name", ["core", "workflow", "runtime", "host", "provider"])
@pytest.mark.parametrize("replacement", [False, True])
def test_ER05_missing_or_copied_history_never_adopted(f, name, replacement):
    path = Path(f.history.paths[name])
    saved = path.with_suffix(".saved")
    path.rename(saved)
    if replacement:
        shutil.copyfile(saved, path)
        path.chmod(0o600)
    with pytest.raises((WorkError, OSError, sqlite3.Error)):
        f.recover()
    if not replacement:
        assert not path.exists()
    else:
        assert path.stat().st_ino != saved.stat().st_ino
    if path.exists():
        path.unlink()
    saved.rename(path)
    assert len(f.rows()["runs"]) == 1


@pytest.mark.parametrize("change", ["missing", "copied", "endpoint", "live"])
def test_ER06_owner_must_be_original_and_stopped(f, change):
    if change == "missing":
        f.old_record.unlink()
    elif change == "copied":
        old = f.old_record.read_bytes()
        f.old_record.rename(f.old_record.with_suffix(".saved"))
        f.old_record.write_bytes(old)
        f.old_record.chmod(0o600)
    elif change == "endpoint":
        f.old_endpoint.touch(mode=0o600)
    elif change == "live":
        # Authored live-owner observation preserves the saved descriptor binding.
        row = f.store.read(f.ref, f.r.scope)
        descriptor = row["recovery_history"]["descriptor"]
        descriptor.update(pid=os.getpid(), start=process_start(os.getpid()))
        write_private(f.old_record, descriptor, replace=True)
        row["recovery_history"]["owner"] = file_binding(f.old_record)
        body = {k: v for k, v in row.items() if k not in {"id", "owner", "steps"}}
        with f.store.transaction() as conn:
            conn.execute("UPDATE runs SET body=? WHERE id=?", (json.dumps(body), f.ref))
    with pytest.raises((WorkError, OSError)):
        f.recover()
    assert len(f.rows()["runs"]) == 1


def test_ER07_concurrent_independent_controllers_publish_once(f):
    drivers = [f.new_driver() for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda d: f.service(d).recover_expired(f.authorization), drivers))
    assert len(set(results)) == 1
    assert len(f.rows()["runs"]) == 2
    assert len(f.rows()["recoveries"]) == 1


class Fault:
    def __init__(self, point):
        self.point = point

    def hit(self, point):
        if point == self.point:
            raise RuntimeError("retained injected interruption")


def test_ER07_restart_after_lost_reply_returns_committed_successor(f):
    with pytest.raises(RuntimeError, match="injected"):
        f.service(f.new_driver(Fault("after:recovery"))).recover_expired(f.authorization)
    original = f.rows()
    f.store = WorkflowStore(f.store.path)
    f.r.now += 500
    f.mutate_task(human_hold=True)
    result = f.service(f.new_driver()).recover_expired(f.authorization)
    assert result == original["recoveries"][0][1]
    assert f.rows() == original
    with pytest.raises(IdempotencyConflict):
        f.recover(replace(f.authorization, operation_key="different-key"))


def test_ER08_interruption_before_commit_preserves_history(f):
    original = f.rows()
    with pytest.raises(RuntimeError, match="injected"):
        f.service(f.new_driver(Fault("before:recovery"))).recover_expired(f.authorization)
    assert f.rows() == original
    assert f.recover()


def test_ER09_legacy_missing_witness_is_not_backfilled(f):
    row = f.store.read(f.ref, f.r.scope)
    body = {
        k: v
        for k, v in row.items()
        if k not in {"id", "owner", "steps", "task_version", "plan", "recovery_history"}
    }
    with f.store.transaction() as conn:
        conn.execute("UPDATE runs SET body=? WHERE id=?", (json.dumps(body), f.ref))
    before = f.rows()
    with pytest.raises(WorkError, match="reconciliation"):
        f.recover()
    assert f.rows() == before


def test_ER09_schema1_migration_preserves_all_original_records(f):
    legacy = f.root / "legacy.sqlite"
    source = f.rows()
    with sqlite3.connect(legacy) as conn:
        conn.executescript("""
            CREATE TABLE workflow_meta(version INTEGER,store_id TEXT);
            CREATE TABLE runs(id TEXT PRIMARY KEY,scope TEXT,task_id TEXT,operation_key TEXT,
              body TEXT,owner TEXT,UNIQUE(scope,task_id),UNIQUE(scope,operation_key));
            CREATE TABLE steps(run_id TEXT,name TEXT,payload TEXT,result TEXT,
              PRIMARY KEY(run_id,name));
            CREATE TABLE lineage(resource TEXT PRIMARY KEY,task_id TEXT,attempt_id TEXT,body TEXT);
        """)
        conn.execute("INSERT INTO workflow_meta VALUES(1,?)", (str(uuid4()),))
        for row in source["runs"]:
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", row)
        conn.execute("INSERT INTO steps VALUES(?,?,?,?)", (f.ref, "start", '{"exact":  1}', None))
        conn.execute("INSERT INTO lineage VALUES(?,?,?,?)", ("other", "other", "prior", '{"x": 2}'))
        expected = {
            t: conn.execute("SELECT * FROM " + t).fetchall() for t in ("runs", "steps", "lineage")
        }
    legacy.chmod(0o600)
    identity = legacy.stat().st_ino
    migrated = WorkflowStore(legacy)
    assert migrated.path.stat().st_ino == identity
    with migrated.transaction() as conn:
        for table, rows in expected.items():
            assert [tuple(r) for r in conn.execute("SELECT * FROM " + table)] == rows
    assert migrated.find(f.r.scope, f.task.id, "original-key") == f.ref
    assert WorkflowStore(legacy).store_id == migrated.store_id


def test_ER10_public_registry_has_no_recovery_authority(f):
    assert not any("recover" in name for name in SCHEMAS)
    with pytest.raises(AccessDenied):
        f.service().recover_expired(asdict(f.authorization))
    successor = f.recover()
    f.mutate_task(human_hold=True)
    result = f.service().resume(successor)
    assert result["data"]["reason"] == "human_or_assignment_change"


def test_ER03_unexpired_original_is_not_replaceable(f):
    f.r.now = f.old_plan.deadline - 1
    with pytest.raises(AccessDenied, match="expired original"):
        f.recover()


def test_ER04_resource_lineage_refuses(f):
    with f.store.transaction() as conn:
        conn.execute("INSERT INTO lineage VALUES(?,?,?,?)", ("branch", f.task.id, "old", "{}"))
    with pytest.raises(AccessDenied, match="lineage"):
        f.recover()


def test_ER04_other_session_task_resource_obligation_refuses(f):
    other = f.r.queued(code=False)
    with f.r.db.transaction() as conn:
        attempts.insert(conn, Attempt("other", other.id, f.driver.session_id, "host", "failed", 0))
        conn.execute("INSERT INTO resource_leases VALUES('database','retained','other')")
    with pytest.raises(WorkError, match="resources"):
        f.recover()


@pytest.mark.parametrize("state", ["pending", "leased", "uncertain", "unknown"])
def test_ER05_provider_progress_obligation_refuses(f, state):
    from ai_task_manager.adapters.linear.progress_store import ProgressStore

    progress = ProgressStore(f.provider)
    row = progress.submit(f.r.scope, f.task.id, "old-progress", {"text": "authored"})
    with f.provider.transaction() as conn:
        conn.execute("UPDATE progress_publications SET state=? WHERE id=?", (state, row["id"]))
    with pytest.raises(WorkError, match="progress"):
        f.recover()


@pytest.mark.parametrize("state", ["received", "leased", "conflict"])
def test_ER05_unapplied_provider_observation_refuses(f, state):
    with f.provider.transaction() as conn:
        conn.execute(
            "INSERT INTO deliveries(id,scope,delivery_id,raw_digest,semantic_digest,"
            "payload,ingress_ref,state) VALUES(?,?,?,?,?,?,?,?)",
            (
                "delivery",
                encoded(asdict(f.r.scope)),
                "external",
                "raw",
                "semantic",
                "{}",
                "ref",
                state,
            ),
        )
    with pytest.raises(WorkError, match="observations"):
        f.recover()


@pytest.mark.parametrize("state", ["confirmed", "failed"])
def test_ER05_settled_provider_history_is_retained(f, state):
    with f.provider.transaction() as conn:
        conn.execute(
            "INSERT INTO sends VALUES(?,?,?,?,?,NULL,NULL,NULL,NULL)",
            ("send", encoded(asdict(f.r.scope)), "op", 1, state),
        )
        before = [tuple(row) for row in conn.execute("SELECT * FROM sends")]
    assert f.recover()
    with f.provider.transaction() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM sends")] == before


def test_ER06_unknown_process_result_refuses(f, monkeypatch):
    import ai_task_manager.adapters.workflow_control.recovery as module

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="observation failed"),
    )
    with pytest.raises(WorkError, match="unknown"):
        f.recover()


def test_ER07_concurrent_different_requests_conflict(f):
    def request(key):
        try:
            return f.service(f.new_driver()).recover_expired(
                replace(f.authorization, operation_key=key)
            )
        except IdempotencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ["first-key", "second-key"]))
    assert results.count("conflict") == 1
    assert len(f.rows()["runs"]) == 2


def test_ER07_actual_process_death_after_commit_keeps_one_successor(f):
    import multiprocessing

    class ExitAfterCommit:
        def hit(self, point):
            if point == "after:recovery":
                os._exit(23)

    def child():
        f.service(f.new_driver(ExitAfterCommit())).recover_expired(f.authorization)

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=10)
    try:
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    f.store = WorkflowStore(f.store.path)
    successor = f.service(f.new_driver()).recover_expired(f.authorization)
    assert f.rows()["recoveries"][0][1] == successor
    assert len(f.rows()["runs"]) == 2


def test_ER08_canonical_hold_waits_for_recovery_boundary_then_fences_successor(f):
    import threading

    requested, held = threading.Event(), threading.Event()

    def hold():
        requested.set()
        f.mutate_task(human_hold=True)
        held.set()

    class AtBoundary:
        def hit(self, point):
            if point == "before:recovery":
                thread.start()
                assert requested.wait(1)
                assert not held.is_set()

    thread = threading.Thread(target=hold)
    successor = f.service(f.new_driver(AtBoundary())).recover_expired(f.authorization)
    thread.join(timeout=5)
    assert held.is_set()
    assert f.service().resume(successor)["data"]["reason"] == "human_or_assignment_change"


def test_ER08_missing_history_at_publication_rolls_back(f):
    path = Path(f.provider.path)
    saved = path.with_suffix(".saved")

    class ReplaceAtBoundary:
        def hit(self, point):
            if point == "before:recovery":
                path.rename(saved)

    before = f.rows()
    try:
        with pytest.raises((WorkError, OSError, sqlite3.Error)):
            f.service(f.new_driver(ReplaceAtBoundary())).recover_expired(f.authorization)
        assert f.rows() == before
        assert not path.exists()
    finally:
        saved.rename(path)


def test_ER10_successor_saves_ordinary_start_intent_before_any_effect(f):
    successor = f.recover()
    result = f.new_driver(Fault("before:start")).run(successor)
    assert result["state"] == "blocked"
    row = f.store.read(successor, f.r.scope)
    assert row["steps"]["start"]["result"] is None
    assert row["attempt_id"] is None
    with f.r.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["original-owner", "current-owner", "configuration"])
def test_ER08_changed_owner_binding_at_publication_refuses(f, name):
    from ai_task_manager.config.files import read_private

    target = {
        "original-owner": f.old_record,
        "current-owner": Path(read_private(f.config)["workflow_control"]["record"]),
        "configuration": f.config,
    }[name]
    saved = target.with_suffix(".saved")

    class ReplaceAtBoundary:
        def hit(self, point):
            if point == "before:recovery":
                target.rename(saved)
                shutil.copyfile(saved, target)
                target.chmod(0o600)

    before = f.rows()
    try:
        with pytest.raises(WorkError, match="changed"):
            f.service(f.new_driver(ReplaceAtBoundary())).recover_expired(f.authorization)
        assert f.rows() == before
    finally:
        target.unlink()
        saved.rename(target)
