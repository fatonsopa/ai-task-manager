"""Scoped review regressions: bounded canonical reads and saved pre-begin intent recovery."""

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
