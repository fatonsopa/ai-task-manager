import multiprocessing
import os
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.config.claim_store import (
    enroll_claim_store,
    open_claim_store,
    verify_claim_store,
)
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, SchemaError
from ai_task_manager.domains.work.existing import ExistingBlocked
from ai_task_manager.storage.database import Database

from .helpers import ExistingRig


def process_admit(root, endpoint, issue, session, barrier, queue):
    try:
        rig = ExistingRig(Path(root), SimpleNamespace(endpoint=endpoint), session=session)
        packet = rig.packet(issue)
        barrier.wait(timeout=10)
        task = rig.intake.admit(*packet, str(uuid4()))
        queue.put(("ok", task.id))
    except AccessDenied as error:
        queue.put(("denied", str(error)))
    except BaseException as error:
        queue.put(("unexpected", repr(error)))


def test_ea42_ea43_processes_share_one_canonical_transactional_claim(rig, provider):
    issue = provider.seed()
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=process_admit,
            args=(
                str(rig.root),
                provider.endpoint,
                issue,
                f"process-{number}",
                barrier,
                queue,
            ),
        )
        for number in range(2)
    ]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
        assert all(process.exitcode == 0 for process in processes), results
        assert sorted(result[0] for result in results) == ["denied", "ok"], results
        effects = rig.effects()
        assert (
            len(effects["tasks"]) == len(effects["issue_bindings"]) == len(effects["events"]) == 1
        )
        assert not effects["attempts"] and not effects["resource_leases"]
        assert "Verify task ownership" not in results[[r[0] for r in results].index("denied")][1]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        queue.close()
        queue.join_thread()


def test_ea42_two_threads_with_different_sessions_have_one_owner(rig, provider):
    issue = provider.seed()
    other = ExistingRig(
        rig.root, provider, session="thread-session", db=rig.db, authority=rig.authority
    )
    prepared = [(owner, owner.packet(issue)) for owner in (rig, other)]
    barrier = threading.Barrier(2)

    def admit(item):
        owner, packet = item
        barrier.wait(timeout=10)
        try:
            return "ok", owner.intake.admit(*packet, str(uuid4())).id
        except AccessDenied:
            return "denied", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, prepared))
    assert sorted(row[0] for row in results) == ["denied", "ok"]
    assert len(rig.effects()["tasks"]) == len(rig.effects()["issue_bindings"]) == 1


def test_ea47_reassignment_and_hidden_task_never_release_unresolved_leases(rig, provider):
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.application.linear_sync.outbound import OutboundDrain
    from ai_task_manager.domains.work.models import Evidence
    from tests.domains.helpers import ProofFixture
    from tests.linear.helpers import uid

    from .test_remaining_guards import queue_todo

    issue = provider.seed("Todo")
    task = queue_todo(rig, rig.admit(issue))
    runtime = ProofFixture()
    rig.core.context.evidence = EvidenceRouter(
        [
            *rig.router.routes,
            EvidenceRoute(
                "fixture-runtime:",
                frozenset({"worker_start", "resources_ready"}),
                runtime,
            ),
        ]
    )

    def authored_runtime(kind, subject, payload):
        ref = "fixture-runtime:" + str(uuid4())
        runtime.evidence[ref] = Evidence(kind, rig.scope, subject, payload)
        return ref

    rig.intake.guard(task, startup=True)
    attempt = rig.core.execution.reserve(
        rig.handle,
        task.id,
        "fixture",
        task.version,
        task.decision_revision,
        rig.decision(task, "In Progress"),
        str(uuid4()),
    )
    task = rig.core.get_task(rig.handle, task.id)
    attempt = rig.core.execution.start(
        rig.handle,
        attempt.id,
        task.version,
        task.decision_revision,
        authored_runtime(
            "worker_start",
            attempt.id,
            {"task_id": task.id, "host": "fixture", "worker_id": "authored-fixture-worker"},
        ),
        rig.decision(task, "In Progress"),
        str(uuid4()),
    )
    task = OutboundDrain(rig.core, rig.client, rig.journal).confirm_pending(rig.handle, task.id)
    attempt = rig.core.execution.running(
        rig.handle,
        attempt.id,
        task.version,
        task.decision_revision,
        authored_runtime(
            "resources_ready",
            attempt.id,
            {
                "task_id": task.id,
                "host": "fixture",
                "worker_id": attempt.worker_id,
                "ready": True,
                "worktree_id": "authored-worktree-identity",
                "database_id": "authored-database-identity",
            },
        ),
        str(uuid4()),
    )
    resources = rig.effects()["resource_leases"]
    assert (
        len(resources) == 2
    )  # Actual Core leases from authored runtime facts, no real host claim.
    other = ExistingRig(
        rig.root, provider, session="other-owner", db=rig.db, authority=rig.authority
    )
    with pytest.raises(AccessDenied):
        other.admit(issue)
    provider.issues[issue]["assignee"] = {"id": uid(999)}
    with pytest.raises(ExistingBlocked):
        rig.intake.guard(rig.core.get_task(rig.handle, task.id), startup=True)
    with pytest.raises(AccessDenied):
        other.admit(issue)
    assert rig.effects()["resource_leases"] == resources
    assert rig.core.get_attempt(rig.handle, attempt.id) == attempt


def configuration(rig, path, database=None):
    from ai_task_manager.config.sessions import SessionFile

    SessionFile(rig.root / "session.json").save(rig.handle, rig.scope)
    write_private(
        path,
        {
            "version": 1,
            "core_database": str(database or rig.db.path),
            "authority_database": str(rig.authority.path),
            "authority_key": str(rig.authority.key_path),
            "session_file": str(rig.root / "session.json"),
            "approval_policy": {
                "writer_hash": "a" * 64,
                "reference_hash": "b" * 64,
                "style_hash": "c" * 64,
            },
        },
    )
    return path


def test_ea65_unshared_and_unenrolled_store_refused_at_product_composition(rig, tmp_path):
    other = Database(rig.root / "unshared.sqlite")
    os.chmod(other.path, 0o600)
    config = configuration(rig, rig.root / "unshared.json", other.path)
    with pytest.raises(AccessDenied):
        load_service(config)
    with other.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    journal = AuthorityJournal(rig.root / "new-authority.sqlite", rig.root / "new-authority.key")
    with pytest.raises(AccessDenied):
        open_claim_store(other.path, journal)
    with pytest.raises(AccessDenied):
        enroll_claim_store(other, rig.authority)


def test_ea66_copy_same_internal_uuid_cannot_replace_enrolled_file(rig):
    copied = rig.root / "copy.sqlite"
    shutil.copy2(rig.db.path, copied)
    with pytest.raises(AccessDenied):
        open_claim_store(str(copied), rig.authority)
    old = rig.root / "old.sqlite"
    os.rename(rig.db.path, old)
    os.rename(copied, rig.db.path)
    with pytest.raises(AccessDenied):
        rig.core.list_tasks(rig.handle)
    with pytest.raises(AccessDenied):
        verify_claim_store(rig.db, rig.authority)


def test_ea67_same_file_rename_preserves_identity_but_no_automatic_replacement(rig):
    renamed = rig.root / "renamed.sqlite"
    os.rename(rig.db.path, renamed)
    database, guard = open_claim_store(str(renamed), rig.authority)
    assert guard.context == rig.claim.context
    with database.transaction() as conn:
        assert database.identity(conn) == rig.claim.context["store_id"]
    replacement = Database(rig.root / "replacement.sqlite")
    os.chmod(replacement.path, 0o600)
    with pytest.raises(AccessDenied):
        enroll_claim_store(replacement, rig.authority)


@pytest.mark.parametrize("when", ["before_open", "after_begin", "before_commit"])
def test_ea71_actual_connection_transaction_refuses_store_replacement(rig, when):
    path, backup = Path(rig.db.path), rig.root / "original.sqlite"
    template = rig.root / "template.sqlite"
    shutil.copy2(path, template)

    def replace_file():
        os.rename(path, backup)
        shutil.copy2(template, path)

    if when == "before_open":
        replace_file()
    elif when == "after_begin":
        original = rig.db.guard
        calls = 0

        def guarded(conn):
            nonlocal calls
            calls += 1
            if calls == 1:
                replace_file()
            original(conn)

        rig.db.guard = guarded
    with pytest.raises(AccessDenied):
        with rig.db.transaction() as conn:
            conn.execute("INSERT INTO issue_lifecycle VALUES('test-claim',0,0,'fixture')")
            if when == "before_commit":
                replace_file()
    for file in (path, backup):
        if file.exists():
            with sqlite3.connect(file) as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM issue_lifecycle WHERE identity_key='test-claim'"
                    ).fetchone()[0]
                    == 0
                )
                assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_ea68_invocations_are_bound_to_installation_not_just_scope(rig, provider, tmp_path):
    from ai_task_manager.application.service.invocations import Invocation

    one = Invocation(
        rig.authority, rig.scope, "task_admit", "same-key", {"a": 1}, claim_store=rig.claim.context
    )
    one.finish({"ok": True, "data": {"private": "owned history"}})
    other = ExistingRig(tmp_path / "other-installation", provider)
    two = Invocation(
        rig.authority,
        rig.scope,
        "task_admit",
        "same-key",
        {"a": 1},
        claim_store=other.claim.context,
    )
    assert two.cached() is None and one.cached()["data"]["private"] == "owned history"
    issue = provider.seed()
    ref, prose, approved, selected = rig.packet(issue)
    other.core.context.evidence = rig.router
    with pytest.raises(EvidenceRequired):
        other.core.existing.admit(
            other.handle, ref, ref, prose, approved, selected, "different-store"
        )


def test_ea68_copied_history_cannot_replay_admission_in_a_new_installation(rig, provider):
    from ai_task_manager.application.core import Core
    from ai_task_manager.domains.work.errors import IdempotencyConflict

    issue = provider.seed()
    ref, prose, approval, selection = rig.packet(issue)
    rig.intake.admit(ref, prose, approval, selection, "old-installation-operation")
    cloned = rig.root / "copied-history.sqlite"
    shutil.copy2(rig.db.path, cloned)
    database = Database(cloned)
    authority = AuthorityJournal(rig.root / "new-install.sqlite", rig.root / "new-install.key")
    enroll_claim_store(database, authority)
    verify_claim_store(database, authority)
    core = Core(database, rig.policy)
    with pytest.raises(IdempotencyConflict):
        core.existing.recover(rig.handle, ref, approval, selection, "old-installation-operation")
    with database.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_ea54_unknown_schema_is_not_repaired(tmp_path):
    path = tmp_path / "future.sqlite"
    database = Database(path)
    with database.transaction() as conn:
        conn.execute("UPDATE metadata SET version=999")
    with pytest.raises(SchemaError):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 999


def test_ea54_corrupt_canonical_lifecycle_row_is_refused(rig, provider):
    from ai_task_manager.storage import issue_claims

    issue = provider.seed()
    with rig.db.transaction() as conn:
        conn.execute(
            "INSERT INTO issue_lifecycle VALUES(?,2,0,'corrupt-fixture')",
            (issue_claims.identity(rig.scope, issue),),
        )
        with pytest.raises(SchemaError):
            issue_claims.require_open(conn, rig.scope, issue)
