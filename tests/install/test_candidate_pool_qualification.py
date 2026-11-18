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
