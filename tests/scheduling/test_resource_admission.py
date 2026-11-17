"""Real Core transactions cover declared conflicts and retained unknown owners."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace

import pytest

from ai_task_manager.application.core import Core
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.domains.scheduling.resources import RejectResourceDeclarations
from ai_task_manager.domains.work.errors import CapacityExceeded, EvidenceRequired
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage.database import Database

from .test_guards import reserve


def declaration(rig, task, *, files=(), locks=(), repository="owned-repository"):
    rig.resource_sources.rows[task.id] = replace(
        rig.resource_sources.rows[task.id], repository=repository, files=files, locks=locks,
    )


def other_session(rig):
    scope = replace(rig.scope, logical_session="second-queue")
    handle = rig.session(scope)
    task = rig.task(handle=handle, confirmed=False)
    task = rig.confirm(task, handle=handle, scope=scope)
    task = rig.core.transitions.transition(handle, task.id, S.TODO, "enqueue", task.version,
        task.decision_revision, rig.decision(task, S.TODO, scope=scope), {}, rig.key())
    return handle, scope, rig.confirm(task, handle=handle, scope=scope)


@pytest.mark.parametrize("kind", ["file", "migration", "test", "resource"])
def test_two_sessions_race_one_conflict_without_disclosing_owner(rig, kind):
    first = rig.queued()
    handle, scope, second = other_session(rig)
    args = {"files": ("src/owned.txt",)} if kind == "file" else {"locks": ((kind, "owned-lock"),)}
    declaration(rig, first, **args)
    declaration(rig, second, **args)
    barrier = Barrier(2)
    cores = [rig.new_core(), rig.new_core()]
    rows = [(rig.handle, rig.scope, first), (handle, scope, second)]
    refs = [rig.decision(t, S.IN_PROGRESS, scope=s) for h, s, t in rows]
    keys = [rig.key(), rig.key()]
    def claim(i):
        h, _, t = rows[i]
        barrier.wait(timeout=5)
        try:
            return cores[i].execution.reserve(h, t.id, "host", t.version,
                                             t.decision_revision, refs[i], keys[i])
        except CapacityExceeded as error:
            assert str(error) == "Declared task resources are occupied"
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))
    assert sum(v is not None for v in results) == 1
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM resource_leases WHERE kind='admission'").fetchone()[0] == 1


def test_conflicting_head_waits_while_independent_task_advances_without_false_bypass(rig):
    owner, head, independent = rig.queued(), rig.queued(), rig.queued()
    declaration(rig, owner, files=("src/shared.txt",))
    declaration(rig, head, files=("src/shared.txt",))
    declaration(rig, independent, files=("src/independent.txt",))
    reserve(rig, owner)
    with pytest.raises(CapacityExceeded):
        reserve(rig, head)
    view = rig.core.scheduling.explain(rig.handle, head.id)
    assert view["selection_state"] == "waiting" and view["waiting_reason"] == "resource_conflict"
    assert view["bypasses"] == 0
    assert rig.core.scheduling.ordered(rig.handle)[0]["task_id"] == independent.id
    assert reserve(rig, independent).task_id == independent.id
    assert rig.core.scheduling.explain(rig.handle, head.id)["bypasses"] == 0


@pytest.mark.parametrize("fault", ["missing", "stale_prose", "foreign_scope"])
def test_unknown_or_stale_declarations_do_not_mean_empty(rig, fault):
    task = rig.queued()
    if fault == "missing":
        rig.core.execution.resource_sources = RejectResourceDeclarations()
    else:
        value = rig.resource_sources.read(rig.scope, task)
        value = replace(value, **({"prose_digest": "foreign"} if fault == "stale_prose"
                                 else {"scope": replace(rig.scope, principal="foreign")}))
        rig.core.execution.resource_sources = SimpleNamespace(read=lambda scope, task: value)
    with pytest.raises(EvidenceRequired):
        reserve(rig, task)
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_unknown_legacy_active_owner_blocks_new_work_and_new_start(rig):
    owner, next_task = rig.queued(), rig.queued()
    attempt = reserve(rig, owner)
    with rig.db.transaction() as conn:
        conn.execute("DELETE FROM resource_leases WHERE kind='admission'")
    rig.db = Database(rig.db.path)
    rig.core = rig.new_core()
    with pytest.raises(CapacityExceeded, match="ownership is unresolved"):
        reserve(rig, next_task)
    with pytest.raises(EvidenceRequired, match="Original atomic resource reservation"):
        rig.core.execution.require_reserved_resources(rig.handle, attempt.id)
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "reserved"


def test_separate_public_service_reads_shared_facts_without_execution_authority(rig):
    task = rig.queued()
    declaration(rig, task, files=("src/owned.txt",))
    rig.core.execution.publish_resources(task)
    reader = Core(Database(rig.db.path), rig.policy, clock=lambda: rig.now)
    service = ApplicationService(reader, rig.handle)
    view = service.call("task_show", {"task_id": task.id})
    assert view["ok"] and view["data"]["scheduling"]["selection_state"] == "ready"
    with pytest.raises(EvidenceRequired):
        reserve(rig, task, core=reader)


@pytest.mark.parametrize("partial", [False, True])
def test_partial_setup_keeps_unknown_claim_until_confirmed_stop(rig, partial):
    task = rig.queued()
    attempt = reserve(rig, task)
    task = rig.current(task)
    if partial:
        with rig.db.transaction() as conn:
            conn.execute("INSERT INTO resource_leases VALUES(?,?,?)", ("database", "owned-partial", attempt.id))
    task, attempt = rig.settle(task, attempt, "unknown", quiescent=False)
    with rig.db.transaction() as conn:
        expected = 2 if partial else 1
        assert conn.execute("SELECT count(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)).fetchone()[0] == expected
    task, attempt = rig.settle(task, attempt, "failed", quiescent=True)
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)).fetchone()[0] == 0
