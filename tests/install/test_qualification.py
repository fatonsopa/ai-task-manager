"""Changed fixture boundaries only; no genuine-effect or delivery claim."""

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "qualification_under_test", Path(__file__).with_name("qualification.py")
)
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


class QualificationControl(unittest.TestCase):
    def test_actual_schema_profiles_crash_refusal_and_diagnostics(self):
        from jsonschema import Draft202012Validator, ValidationError

        from ai_task_manager.adapters.hosts.bridge.admission import role_schemas
        from ai_task_manager.adapters.hosts.bridge.conformance.models import cases

        plan = {
            "roles": {"c_author": {}, "b_author": {}},
            "operation_keys": {"c": "original-c"},
            "consumer_counts": {"workers": 2, "result_leads": 1, "queue_leads": 0},
        }
        self.assertEqual(q.required_profiles(plan), ("worker", "lead"))
        more = copy.deepcopy(plan)
        more["consumer_counts"]["queue_leads"] = 1
        self.assertEqual(q.required_profiles(more), ("worker", "lead", "queue_lead"))
        ctx = SimpleNamespace(reference="controlled-probe", case_ids=("allow_write",))
        case = cases(ctx)[0]
        self.assertIn("expected_hash", case.arguments)
        self.assertIsNone(case.arguments["expected_hash"])
        schema = role_schemas("worker", ())["write_file"]
        Draft202012Validator(schema).validate(case.arguments)
        malformed = dict(case.arguments)
        malformed.pop("expected_hash")
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(malformed)
        self.assertEqual(set(role_schemas("lead", ())), {"acknowledge_event"})
        members = {
            n: {
                "task_id": n,
                "installed_task_id": n,
                "issue_id": n + "-issue",
                "receipt_issue_id": n + "-issue",
                "scope_matches": True,
                "permission_ref": n + "-permission",
                "all_calls_stopped": True,
            }
            for n in plan["roles"]
        }
        values = {
            "closed": None,
            "original_exit": {
                "actual_handle": True,
                "returncode": 71,
                "pid": 42,
                "start_identity": "owned-birth",
            },
            "original_launch": {"pid": 42, "start_identity": "owned-birth"},
            "marker": {
                "pid": 42,
                "exit_code": 71,
                "physical_author_stopped": True,
                "task_id": "c_author",
                "operation_key": "original-c",
                "workflow_ref": "workflow:original-c",
            },
            "replacement": {
                "returncode": 0,
                "signal_via_retained_Popen": False,
                "actual_child_pid": 43,
            },
            "command_result": {"actual_child_pid": 43, "actual_child_handle_retained": True},
            "stopped": {
                "ok": True,
                "outcome": "confirmed",
                "data": {
                    "workflow": "workflow:original-c",
                    "state": "done",
                    "quiescent": True,
                    "components": dict.fromkeys(
                        ["native", "commands", "merge", "harness", "collectors"], True
                    ),
                },
            },
            "members": members,
            "active_attempts": 0,
            "leases": 0,
            "current_authority": True,
        }
        self.assertEqual(q.disposition(plan, **values), "crash_cold")
        for key, bad in [
            ("original_exit", None),
            ("leases", 1),
            ("active_attempts", 1),
            ("current_authority", False),
            ("members", {}),
        ]:
            changed = copy.deepcopy(values)
            changed[key] = bad
            with (
                self.subTest(missing=key),
                self.assertRaises((AssertionError, TypeError, KeyError)),
            ):
                q.disposition(plan, **changed)
        for path, bad in [
            (("original_exit", "returncode"), 0),
            (("original_launch", "start_identity"), "foreign"),
            (("replacement", "returncode"), 1),
            (("members", "b_author", "all_calls_stopped"), False),
            (("members", "b_author", "receipt_issue_id"), "foreign"),
            (("stopped", "data", "components", "native"), False),
        ]:
            changed = copy.deepcopy(values)
            v = changed
            for key in path[:-1]:
                v = v[key]
            v[path[-1]] = bad
            with self.subTest(boundary=path), self.assertRaises(AssertionError):
                q.disposition(plan, **changed)
        graceful = copy.deepcopy(values)
        graceful.update(
            closed={
                "ok": True,
                "outcome": "confirmed",
                "data": {
                    "state": "owner_closed",
                    "shutdown": [{"quiescent": True, "state": "done"}],
                },
            },
            original_exit={**values["original_exit"], "returncode": 0},
        )
        self.assertEqual(q.disposition(plan, **graceful), "graceful")
        identities = {n: (n, n + "-issue") for n in members}
        protected = [{"role": "c_author", "task_id": "c_author", "issue_id": "c_author-issue"}]
        self.assertEqual(q.verified_protection(identities, protected, []), {"c_author-issue"})
        for bad in [
            None,
            [],
            [{"role": "c_author", "task_id": "foreign", "issue_id": "c_author-issue"}],
        ]:
            with self.subTest(protection=bad), self.assertRaises((AssertionError, TypeError)):
                q.verified_protection(identities, bad, [])
        with self.assertRaises(AssertionError):
            q.verified_protection(
                identities,
                protected,
                [{"role": "b_author", "task_id": "b_author", "issue_id": "b_author-issue"}],
            )
        # The actual installed signed-receipt verifier supplies rejection fields.
        from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
        from ai_task_manager.domains.scheduling.models import DependencySnapshot
        from ai_task_manager.domains.work.errors import AccessDenied
        from ai_task_manager.domains.work.models import Scope

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            receipts = DependencyReceipts(base / "r.sqlite", base / "r.key", clock=lambda: 100.0)
            scope = Scope("linear", "workspace", "owner", "project", "session")
            task = SimpleNamespace(
                id="task", issue_id="issue", prose_digest="a" * 64, version=1, decision_revision=0
            )
            first = receipts.begin(scope, task, 0, "one")
            snapshot = DependencySnapshot(
                scope,
                task.id,
                task.prose_digest,
                task.issue_id,
                "linear",
                1,
                100.0,
                110.0,
                "complete",
                "normal",
                (),
            )
            ref = receipts.finish(scope, task.id, "one", first["context"], snapshot, {})
            receipts.begin(scope, task, 0, "two")
            observer = q.ReceiptObserver(
                DependencyReceipts.verify_snapshot.__code__, base / "observed.json"
            )
            try:
                with self.assertRaises(AccessDenied):
                    receipts.verify_snapshot(ref)
            finally:
                observer.close()
            row = json.loads((base / "observed.json").read_text())["events"][0]
            self.assertEqual(
                (row["rejected_reference"], row["source_revision"], row["current_generation"]),
                (ref, 1, 2),
            )
            self.assertTrue(row["callers"])
            self.assertIn("last_boundary", row)

    def test_ended_capture_journal(self):
        # A caller-bound retained artifact; no live authority, SDK or model call.
        from ai_task_manager.adapters.hosts.bridge.journal import HostJournal

        packet_file = os.environ.get("DP06_QUALIFICATION_CAPTURE_PACKET")
        if not packet_file:
            self.skipTest("Bind a retained capture packet to inspect actual ended-case evidence")
        packet = json.loads(Path(packet_file).read_text())
        root = Path(packet["run_root"])
        journal = HostJournal.open_existing(root / "host.sqlite", root / "host.key")
        sub = [
            r
            for r in journal._history("trace")
            if r["payload"].get("effect") == "native_probe_submission"
        ][-1]
        context = SimpleNamespace(reference=packet["run_id"], case_ids=tuple(packet["case_ids"]))
        fake = SimpleNamespace(gateway=SimpleNamespace(case=None, context=context))
        observed = q.capture_rejection(fake, journal)
        self.assertEqual(observed["case_id"], "allow_write")
        self.assertEqual(observed["request_ref"], sub["ref"])
        self.assertTrue(observed["joined_tools"])
        self.assertTrue(
            any("expected_hash" not in use["tool_use"]["input"] for use in observed["joined_tools"])
        )


if __name__ == "__main__":
    unittest.main()
