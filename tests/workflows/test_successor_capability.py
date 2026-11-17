"""Authored Core authority controls; no provider or native qualification claim."""

import secrets
from dataclasses import asdict, replace

import pytest

from ai_task_manager.application.workflows.recovery import SuccessorAuthorization
from ai_task_manager.domains.work.errors import AccessDenied, WorkError
from ai_task_manager.domains.work.models import Evidence, SessionHandle
from tests.domains.helpers import Rig


@pytest.fixture
def admission(tmp_path):
    r = Rig(tmp_path / "core.sqlite")
    selected, foreign = r.queued(code=True, units=("first", "remaining")), r.queued(code=False)
    with r.db.transaction() as conn:
        from ai_task_manager.storage import work
        selected = work.save(conn, replace(selected, completed_work=("first",)), selected.version)
    decision = r.proof("decision", selected.id, {"target": "In Progress", "revision": 0,
                       "actor": "agent"}, expires_at=r.now + 200)
    a = SuccessorAuthorization(r.key(), r.scope, "workflow:old", selected.id,
                               selected.version, 0, "fresh-plan", "digest", "fresh-key",
                               r.now + 100, decision, ("remaining",))
    r.proofs.evidence[a.reference] = Evidence("workflow_successor", r.scope, selected.id,
                                             asdict(a), a.expires_at)
    handle = SessionHandle(secrets.token_urlsafe(32))
    r.core.revoke_session(r.handle)
    return r, selected, foreign, a, handle


def test_new_task_capability_preserves_old_revocation_and_partial_work(admission):
    r, selected, foreign, a, handle = admission
    reference = r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: "workflow:new")
    assert reference == "workflow:new"
    assert r.core.get_task(handle, selected.id).completed_work == ("first",)
    assert [task.id for task in r.core.list_tasks(handle)] == [selected.id]
    assert {op.task_id for op in r.core.sync.list_operations(handle)} == {selected.id}
    assert {event.task_id for event in r.core.notifications.list_events(handle)} == {selected.id}
    with pytest.raises(AccessDenied):
        r.core.get_task(handle, foreign.id)
    with pytest.raises(AccessDenied):
        r.core.get_task(r.handle, selected.id)
    with r.db.transaction() as conn:
        assert conn.execute("SELECT revoked FROM sessions").fetchone()[0] == 1


def test_capability_expiry_and_exact_admission_replay_do_not_renew(admission):
    r, selected, _, a, handle = admission
    r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: "workflow:new")
    r.now = a.expires_at + 1
    with pytest.raises(AccessDenied):
        r.core.get_task(handle, selected.id)
    assert r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: pytest.fail()) == "workflow:new"


def test_held_task_refuses_before_publication(admission):
    r, selected, _, a, handle = admission
    with r.db.transaction() as conn:
        from ai_task_manager.storage import work
        work.save(conn, replace(selected, human_hold=True), selected.version)
    with pytest.raises(WorkError):
        r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: pytest.fail())


def test_partial_successor_publishes_one_head_and_retains_original_history(admission, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from ai_task_manager.application.workflows.models import WorkflowPlan
    from ai_task_manager.application.workflows.recovery import admit_successor
    from ai_task_manager.storage.workflows import WorkflowStore

    r, selected, _, a, handle = admission
    root = tmp_path / "workflow"
    root.mkdir(mode=0o700)
    store = WorkflowStore(root / "workflow.sqlite")
    old = WorkflowPlan("original", "assignment", "fixture", "base", ("result",),
                       ("result",), r.now - 1)
    ref = store.create(r.scope, selected, old, "original-key", "original-runtime")
    store.claim(ref, r.scope, "old-controller")
    store.intend(ref, "old-controller", "execute", {"attempt": "retained-attempt"})
    original = store.read(ref, r.scope)
    plan = replace(old, id="fresh", deadline=r.now + 100)
    a = replace(a, predecessor=ref, plan_ref=plan.id, plan_digest=plan.digest)
    r.proofs.evidence[a.reference] = Evidence("workflow_successor", r.scope, selected.id,
                                             asdict(a), a.expires_at)
    reconciliation = SimpleNamespace(check=lambda *_: None, check_locked=lambda *_: None,
                                      boundary=lambda *_: nullcontext())
    successor = admit_successor(r.core, r.handle, handle, store, a, plan,
                                "fresh-runtime", reconciliation)
    assert store.read(ref, r.scope) == original
    row = store.read(successor, r.scope)
    assert row["succession"]["retained"]["steps"] == original["steps"]
    assert row["succession"]["authorization"]["remaining_work"] == ["remaining"]
    assert store.find(r.scope, selected.id, "original-key") == ref
    assert store.find(r.scope, selected.id, a.operation_key) == successor
    assert admit_successor(r.core, r.handle, handle, store, a, plan,
                           "fresh-runtime", reconciliation) == successor
    assert r.core.get_task(handle, selected.id).completed_work == ("first",)
    with pytest.raises(AccessDenied):
        store.claim(ref, r.scope, "obsolete-owner")
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM recoveries").fetchone()[0] == 1
        assert conn.execute("SELECT run_id FROM workflow_heads").fetchone()[0] == successor
