"""Scoped review regressions: bounded canonical reads and saved pre-begin intent recovery."""

import json

import pytest

from ai_task_manager.storage import merging_fences
from tests.domains.helpers import Rig
from tests.merge.workflows.helpers import Flow


def test_canonical_lookup_does_not_decode_unrelated_publication_history(tmp_path, monkeypatch):
    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task()
    with rig.db.transaction() as conn:
        for number in range(500):
            rig.core.context.changed(
                conn,
                task,
                "publication_fence_updated",
                {
                    "issue_key": str(number).zfill(64),
                    "context": {
                        "reference": "foreign:" + str(number),
                        "binding_digest": "a" * 64,
                        "commit": "b" * 40,
                    },
                    "intent": {},
                    "state": "pending",
                },
            )
        original = merging_fences.json.loads
        decoded = []

        def observe(*args, **kwargs):
            decoded.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(merging_fences.json, "loads", observe)
        assert merging_fences.pending(conn, "f" * 64) == []
        assert len(decoded) <= 1, {"unrelated_history_decodes": len(decoded)}


@pytest.mark.parametrize("mode", ["expired", "missing"])
def test_fully_saved_unapplied_intent_reprepares_exact_current_receipt(tmp_path, mode):
    flow = object.__new__(Flow)
    try:
        flow.__init__(tmp_path / "owned")
        original = flow.r.core.publications.begin

        def interrupted_before_begin(*args, **kwargs):
            raise RuntimeError("Authored interruption before actual Core.begin")

        flow.r.core.publications.begin = interrupted_before_begin
        assert not flow.run()["ok"]
        reference = next(iter(flow.workflow.references))
        row = flow.workflow.store.read(reference)
        saved = row["steps"]["publication_intent"]["result"]
        assert saved is not None and row["steps"]["publication_fence"]["result"] is None
        task = flow.r.core.get_task(flow.r.handle, flow.task.id)
        with flow.r.db.transaction() as conn:
            from ai_task_manager.storage.issue_claims import identity

            assert not merging_fences.pending(conn, identity(flow.r.scope, task.issue_id))
        if mode == "expired":
            flow.r.now += 6
        else:
            flow.evidence.prepared.clear()
        flow.r.core.publications.begin = original
        resumed = flow.resume(reference)
        assert resumed["ok"] and resumed["data"]["state"] == "done", resumed
        assert flow.workflow.store.read(reference)["steps"]["publication_intent"]["result"] == saved
        assert len(flow.executor.records) == 1
    finally:
        if hasattr(flow, "runtime"):
            flow.close()


@pytest.mark.parametrize(
    "change", ["hold", "decision", "scope", "source", "target", "checks", "returned_ref"]
)
def test_saved_intent_refresh_never_adopts_changed_authority_or_inputs(tmp_path, change):
    from dataclasses import replace

    from ai_task_manager.adapters.resources.git import git

    flow = object.__new__(Flow)
    try:
        flow.__init__(tmp_path / "owned")
        begin = flow.r.core.publications.begin

        def before_begin(*args, **kwargs):
            raise RuntimeError("Authored before-Core.begin interruption")

        flow.r.core.publications.begin = before_begin
        assert not flow.run()["ok"]
        ref = next(iter(flow.workflow.references))
        saved = flow.workflow.store.read(ref)["steps"]["publication_intent"]["result"]
        flow.r.core.publications.begin = begin
        flow.evidence.prepared.clear()
        prepare = flow.evidence.prepare_intent
        calls = []

        def observed_prepare(*args):
            calls.append(1)
            actual = prepare(*args)
            return "merge-publication:intent:foreign" if change == "returned_ref" else actual

        flow.evidence.prepare_intent = observed_prepare
        if change == "hold":
            flow.runtime.hold(flow.dispatch)
        elif change == "decision":
            task = flow.r.core.get_task(flow.r.handle, flow.task.id)
            proof = flow.r.proof(
                "human_decision",
                task.id,
                {"previous_revision": task.decision_revision, "hold": False},
            )
            flow.r.core.decisions.record(flow.r.handle, task.id, task.version, proof, flow.r.key())
        elif change == "scope":
            flow.workflow.plan = replace(
                flow.plan,
                grant=replace(flow.grant, binding=replace(flow.binding, principal_id="foreign")),
            )
        elif change == "source":
            git(flow.tree, "update-ref", flow.grant.source_ref, flow.old, flow.new)
        elif change == "target":
            git(flow.tree, "update-ref", "refs/heads/" + flow.target, flow.new, flow.old)
        elif change == "checks":
            flow.guard.source = "0" * 64
        target = git(flow.tree, "rev-parse", "refs/heads/" + flow.target)
        result = flow.resume(ref)
        assert not result["ok"], result
        assert flow.workflow.store.read(ref)["steps"]["publication_intent"]["result"] == saved
        assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == target
        assert not any(
            row["facts"].get("local_git") for row in flow.journal.for_attempt(flow.attempt.id)
        )
        if change in {"hold", "decision", "scope"}:
            assert not calls
        with flow.r.db.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM publication_fences").fetchone()
    finally:
        if hasattr(flow, "runtime"):
            flow.close()


def test_applied_begin_replays_original_context_without_repreparing_or_duplicate_fence(tmp_path):
    from tests.merge.f1_fixtures import FaultOnce

    flow = object.__new__(Flow)
    try:
        flow.__init__(tmp_path / "owned")
        flow.workflow.faults = FaultOnce("after_publication_fence")
        assert not flow.run()["ok"]
        ref = next(iter(flow.workflow.references))
        before = flow.workflow.store.read(ref)
        assert before["steps"]["publication_intent"]["result"] is not None
        assert before["steps"]["publication_fence"]["result"] is None
        with flow.r.db.transaction() as conn:
            original = conn.execute("SELECT context_ref FROM publication_fences").fetchone()[0]
        flow.evidence.prepared.clear()

        def forbidden_reprepare(*args):
            raise AssertionError("Applied begin must replay before private witness lookup")

        flow.evidence.prepare_intent = forbidden_reprepare
        result = flow.resume(ref)
        assert result["data"]["state"] == "done", result
        with flow.r.db.transaction() as conn:
            assert (
                conn.execute("SELECT context_ref FROM publication_fences").fetchone()[0] == original
            )
            states = [
                json.loads(row[0])["state"]
                for row in conn.execute(
                    "SELECT payload FROM events WHERE kind='publication_fence_updated' "
                    "ORDER BY sequence"
                )
            ]
            assert states == ["pending", "committed"]
        assert len(flow.executor.records) == 1
    finally:
        if hasattr(flow, "runtime"):
            flow.close()


def test_projection_and_core_event_rollback_together_before_publication(tmp_path):
    flow = object.__new__(Flow)
    try:
        flow.__init__(tmp_path / "owned")
        changed = flow.r.core.context.changed
        observed = []

        def fail_event(conn, task, kind, *args, **kwargs):
            if kind == "publication_fence_updated":
                observed.append(
                    conn.execute("SELECT count(*) FROM publication_fences").fetchone()[0]
                )
                raise RuntimeError("Authored interruption after projection before typed event")
            return changed(conn, task, kind, *args, **kwargs)

        flow.r.core.context.changed = fail_event
        assert not flow.run()["ok"]
        assert observed == [1]
        with flow.r.db.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM publication_fences").fetchone()
            assert not conn.execute(
                "SELECT 1 FROM events WHERE kind='publication_fence_updated'"
            ).fetchone()
        assert not any(
            row["facts"].get("local_git") for row in flow.journal.for_attempt(flow.attempt.id)
        )
        flow.r.core.context.changed = changed
        result = flow.resume(next(iter(flow.workflow.references)))
        assert result["data"]["state"] == "done", result
    finally:
        if hasattr(flow, "runtime"):
            flow.close()
