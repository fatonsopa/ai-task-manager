"""Only the changed fixture pool mapping and continuation decision."""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pool_qualification", Path(__file__).with_name("qualification.py")
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


class CandidatePoolControl(unittest.TestCase):
    def test_emitted_slots_stages_and_original_target_continuation(self):
        slots = {
            "a": ["code_candidate", "code_candidate_standby"],
            "b": ["code_candidate", "code_candidate_standby"],
            "c": ["code_candidate"],
        }
        plan = {
            "candidate_slots": slots,
            "roles": {g + "_" + n: {} for g in "abc" for n in ("author", "code_worker", *slots[g])},
            "consumer_counts": {"workers": 11, "result_leads": 1, "queue_leads": 0},
        }
        roster = q.preparation_roles(plan)
        self.assertEqual(len(roster), 12)
        self.assertEqual(set(roster), {*plan["roles"], "coordination"})
        for group in "abc":
            names = q.candidate_names(plan, group)
            pointers = {
                n: {"path": group + "/" + n + ".json", "sha256": "a" * 64}
                for n in ("code_worker", *names)
            }
            emitted = q.review_bindings(pointers, names)
            self.assertEqual(q.review_stage(names[-1]), "code_candidate")
            self.assertEqual(type(emitted["code_candidate"]), list if group in "ab" else dict)
            self.assertEqual(
                len(emitted["code_candidate"]) if group in "ab" else 1, 2 if group in "ab" else 1
            )
        event = {
            "state": "blocked",
            "detail": "Source or target changed during immutable preparation",
        }
        facts = dict(
            original_key_matches=True,
            source_unchanged=True,
            target_changed=True,
            publication_steps=[],
            graph_ready=True,
            current_authority=True,
            standby_ready=True,
            elapsed=400,
            wait_seconds=30,
            lineage_count=0,
            already_resumed=False,
        )
        self.assertTrue(q.target_only_resume(event, **facts))
        for key, value in [
            ("original_key_matches", False),
            ("source_unchanged", False),
            ("target_changed", False),
            ("publication_steps", ["publication_intent"]),
            ("graph_ready", False),
            ("current_authority", False),
            ("standby_ready", False),
            ("elapsed", 600),
            ("wait_seconds", 120),
            ("lineage_count", 1),
            ("already_resumed", True),
        ]:
            changed = {**facts, key: value}
            with self.subTest(boundary=key), self.assertRaises(AssertionError):
                q.target_only_resume(event, **changed)
        with self.assertRaises(AssertionError):
            q.target_only_resume({"state": "blocked", "detail": "Different failure"}, **facts)


if __name__ == "__main__":
    unittest.main()


def test_target_context_uses_real_scope_refs_and_current_attempt(tmp_path):
    from types import SimpleNamespace
    from ai_task_manager.adapters.resources.git import git
    from ai_task_manager.storage.workflows import WorkflowStore
    from ai_task_manager.application.workflows.models import WorkflowPlan
    from ai_task_manager.domains.work.errors import WorkError
    from tests.merge.workflows.helpers import Flow

    flow = Flow(tmp_path / "owned")
    try:
        checks = flow.checks.run
        moved = []
        def late_target(*args):
            value = checks(*args)
            target = git(flow.runtime.repo, "commit-tree", flow.old + "^{tree}", "-p", flow.old,
                         "-m", "Independent target before owner preflight")
            git(flow.runtime.repo, "update-ref", "refs/heads/integration", target, flow.old)
            moved.append(target)
            return value
        flow.checks.run = late_target
        result = flow.run()
        assert not result["ok"] and result["error"]["message"] == "Source or target changed during immutable preparation", result
        store = WorkflowStore(flow.root / "outer.sqlite")
        plan = WorkflowPlan("original-plan", "unused", "fixture", flow.old, ("src/a.txt",), ("src/a.txt",), flow.plan.deadline)
        ref = store.create(flow.r.scope, flow.task, plan, "original-key")
        store.claim(ref, flow.r.scope, "original-owner")
        store.intend(ref, "original-owner", "worker-commit", {"attempt_id": flow.attempt.id})
        store.finish(ref, "original-owner", "worker-commit", {"commit": flow.new}, attempt_id=flow.attempt.id)
        store.release(ref, "original-owner")
        delivery = SimpleNamespace(components={ref:flow.workflow}, resources=flow.runtime.resources,
                                   runtime=SimpleNamespace(pair=lambda attempt_id: flow.pair))
        driver = SimpleNamespace(store=store, scope=flow.r.scope, code_delivery=delivery)
        service = SimpleNamespace(core=flow.r.core, handle=flow.r.handle, scope=flow.r.scope,
                                  workflows=SimpleNamespace(driver=driver))
        current = q.target_continuation_context(service, ref)
        assert current["source"] == flow.new and current["target"] == moved[0]
        assert current["view"]["state"] == "ready" and current["attempt"].id == flow.attempt.id
        assert not current["merged"].get("target_lineage")
        # A later current graph cannot revive an attempt invalidated by expiry.
        flow.r.now += 31
        from tests.scheduling.helpers import known_declaration
        known_declaration(flow.r, flow.r.core.get_task(flow.r.handle, flow.task.id))
        assert flow.r.core.scheduling.explain(flow.r.handle, flow.task.id)["state"] == "ready"
        import pytest
        with pytest.raises(WorkError, match="Reconcile the existing attempt"):
            q.target_continuation_context(service, ref)
    finally:
        flow.close()
