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


def test_R01_successor_inbound_is_limited_to_original_task_family(admission):
    from ai_task_manager.storage.work import fingerprint
    r, selected, foreign, a, handle = admission
    # Seed original history before revocation through the retained Core fixture.
    with r.db.transaction() as conn:
        sid = conn.execute("SELECT id FROM sessions").fetchone()[0]
        from ai_task_manager.storage.outbox import ingest
        own = {"data": {"id": selected.issue_id}, "body": "selected"}
        other = {"data": {"id": foreign.issue_id}, "body": "foreign"}
        ingest(conn, sid, "delivery-A", own)
        ingest(conn, sid, "delivery-B", other)
    r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: "workflow:new")
    rows = r.core.sync.inbound(handle)
    assert [(row["delivery_id"], row["payload"]) for row in rows] == [("delivery-A", own)]
    for delivery, payload in (("delivery-A", own), ("delivery-B", other)):
        ingress = r.proof("provider_ingress", delivery, {"payload_digest": fingerprint(payload)})
        applied = r.proof("ingress_applied", delivery, {"payload_digest": fingerprint(payload)})
        if delivery == "delivery-A":
            assert r.core.sync.ingest(handle, delivery, payload, ingress) is False
            r.core.sync.mark_applied(handle, delivery, applied)
        else:
            with pytest.raises(AccessDenied):
                r.core.sync.ingest(handle, delivery, payload, ingress)
            with pytest.raises(AccessDenied):
                r.core.sync.mark_applied(handle, delivery, applied)
    with r.db.transaction() as conn:
        assert conn.execute("SELECT state FROM inbox WHERE delivery_id='delivery-B'").fetchone()[0] == "received"


def test_R01_provider_selection_and_retry_preserve_foreign_receipts(admission, tmp_path):
    import json
    from ai_task_manager.adapters.linear.receipts import ProviderJournal
    from ai_task_manager.application.linear_sync.inbound import InboundDrain
    r, selected, foreign, a, handle = admission
    journal = ProviderJournal(tmp_path / "provider.sqlite")
    rows = []
    for number, task in enumerate((selected, foreign)):
        payload = {"data": {"id": task.issue_id, "updatedAt": "2026-11-17T12:00:00Z"}, "marker": number}
        row, _ = journal.accept_delivery(r.scope, f"delivery-{number}", json.dumps(payload).encode(), payload)
        rows.append(row)
    r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: "workflow:new")
    drain = InboundDrain(r.core, None, journal, clock=lambda: r.now, authorized_actors=())
    assert drain.next_receipt(handle) == rows[0]["id"]
    with journal.transaction() as conn:
        conn.execute("UPDATE deliveries SET state='applied' WHERE id=?", (rows[0]["id"],))
        conn.execute("UPDATE deliveries SET state='conflict' WHERE id=?", (rows[1]["id"],))
    before = journal.deliveries(r.scope)[1]
    assert drain.next_receipt(handle) is None
    assert drain.run_once(handle) is None
    with pytest.raises(AccessDenied):
        drain.retry_conflict(handle, rows[1]["id"])
    assert journal.deliveries(r.scope)[1] == before


def test_R02_predecessor_writer_protocol_cannot_reset_or_mutate_after_admission(admission):
    import sqlite3
    from ai_task_manager.storage.scheduling_schema import register as scheduling
    from ai_task_manager.storage.publication_schema import register as publication
    r, selected, _, a, handle = admission
    r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: "workflow:new")
    with sqlite3.connect(r.db.path) as old:
        scheduling(old); publication(old)
        before = old.execute("SELECT token_hash,revoked FROM sessions").fetchall()
        assert old.execute("SELECT id FROM tasks WHERE id=?", (selected.id,)).fetchone()
        for command, parameters in (("UPDATE sessions SET revoked=0,token_hash='replacement'", ()),
                                    ("UPDATE tasks SET version=version+1 WHERE id=?", (selected.id,))):
            with pytest.raises(sqlite3.OperationalError, match="aitm_successor_writer_protocol"):
                old.execute(command, parameters)
        assert old.execute("SELECT token_hash,revoked FROM sessions").fetchall() == before
    assert r.core.get_task(handle, selected.id).id == selected.id


def test_publication_lost_core_commit_and_concurrent_exact_retry_keep_one_successor(admission, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from types import SimpleNamespace
    from ai_task_manager.application.workflows.models import WorkflowPlan
    from ai_task_manager.application.workflows.recovery import admit_successor
    from ai_task_manager.storage.workflows import WorkflowStore
    r, selected, _, a, handle = admission
    root = tmp_path / "retained"
    root.mkdir(mode=0o700)
    store = WorkflowStore(root / "workflow.sqlite")
    old = WorkflowPlan("old", "assignment", "fixture", "base", ("result",), ("result",), r.now - 1)
    ref = store.create(r.scope, selected, old, "old-key")
    plan = replace(old, id="fresh", deadline=a.expires_at)
    a = replace(a, predecessor=ref, plan_ref=plan.id, plan_digest=plan.digest)
    r.proofs.evidence[a.reference] = Evidence("workflow_successor", r.scope, selected.id, asdict(a), a.expires_at)
    original_admit = r.core.admit_workflow_successor
    def fail(old_handle, new_handle, authority, publish):
        def interrupted(*args):
            publish(*args)
            raise RuntimeError("Lost Core commit after workflow publication")
        return original_admit(old_handle, new_handle, authority, interrupted)
    r.core.admit_workflow_successor = fail
    boundary = SimpleNamespace(check=lambda *_: None, check_locked=lambda *_: None, boundary=lambda *_: nullcontext())
    with pytest.raises(RuntimeError, match="Lost Core commit"):
        admit_successor(r.core, r.handle, handle, store, a, plan, "runtime", boundary)
    recorded = store.recovery_result(r.scope, ref, asdict(a))
    assert recorded is not None
    with pytest.raises(AccessDenied):
        r.core.get_task(handle, selected.id)
    r.core.admit_workflow_successor = original_admit
    def retry(_):
        return admit_successor(r.core, r.handle, handle, store, a, plan, "runtime", boundary)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(retry, range(2))) == [recorded, recorded]
    assert r.core.get_task(handle, selected.id).id == selected.id
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM recoveries").fetchone()[0] == 1
