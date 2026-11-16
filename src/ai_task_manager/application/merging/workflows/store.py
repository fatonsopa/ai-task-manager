"""Workflow intents use the existing coherent journal, never a task-status table."""

from dataclasses import asdict, replace
from uuid import uuid4

from ai_task_manager.adapters.workflow_control.identity import process_start
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied, IdempotencyConflict


class MergeWorkflowStore:
    def __init__(self, journal):
        self.journal = journal

    def create(self, plan, profile, operation_key, *, maximum_attempts=1):
        binding = plan.grant.binding
        key = "merge-workflow:" + digest(
            [
                binding.principal_id,
                binding.project_id,
                binding.session_id,
                binding.task_id,
                profile.source["common_identity"],
            ]
        )
        canonical = replace(
            binding,
            operation_key=key,
            repository_id="physical-repository:" + digest(profile.source["common_identity"]),
            configuration_digest=digest(profile.source["common_identity"]),
        )
        ref = self.journal.register(asdict(canonical), canonical.fingerprint)["ref"]
        row = self.journal.current(ref)
        request = {
            "plan": plan.digest,
            "operation_key": operation_key,
            "grant": asdict(plan.grant),
            "profile": profile.digest,
        }
        if maximum_attempts != 1:
            request["maximum_attempts"] = maximum_attempts
        if "merge_workflow" in row["facts"]:
            if row["facts"]["merge_workflow"]["request"] != request:
                raise IdempotencyConflict("Task has a different installed merge workflow")
        else:
            profile.validate()
            self._save(ref, row, {"request": request, "owner": None, "steps": {}})
        return ref

    def read(self, ref):
        return self.journal.current(ref)["facts"]["merge_workflow"]

    def _save(self, ref, row, facts):
        prior = row["facts"].get("merge_workflow", {}).get("steps", {})
        changed = [
            {"step": name, "result_recorded": step["result"] is not None}
            for name, step in facts["steps"].items()
            if prior.get(name) != step
        ]
        self.journal.commit(
            ref,
            row["version"],
            "workflow:" + str(uuid4()),
            "merge_workflow",
            dict(row["facts"], merge_workflow=facts),
            {"changed_steps": changed},
        )

    def claim(self, ref):
        import os

        row = self.journal.current(ref)
        facts = row["facts"]["merge_workflow"]
        if facts["owner"] is not None:
            raise AccessDenied("Another or unresolved merge controller owns this task")
        owner = {"id": str(uuid4()), "pid": os.getpid(), "birth": process_start(os.getpid())}
        self._save(ref, row, dict(facts, owner=owner))
        return owner

    def release_dead_claim(self, ref, expected, check):
        """Transfer control only after the installed owner verifies its lineage."""
        row = self.journal.current(ref)
        facts = row["facts"]["merge_workflow"]
        if facts["owner"] != expected or expected is None:
            raise AccessDenied("Original merge controller claim changed")
        check(expected)
        self._save(
            ref,
            row,
            dict(facts, owner=None, owner_history=[*facts.get("owner_history", []), expected]),
        )

    def update(self, ref, expected_owner, **changes):
        row = self.journal.current(ref)
        facts = row["facts"]["merge_workflow"]
        if facts["owner"] != expected_owner:
            raise AccessDenied("Canonical merge workflow owner changed")
        self._save(ref, row, dict(facts, **changes))

    def intend(self, ref, owner, name, payload):
        steps = self.read(ref)["steps"]
        if name not in steps:
            self.update(
                ref, owner, steps=dict(steps, **{name: {"payload": payload, "result": None}})
            )

    def finish(self, ref, owner, name, result):
        steps = self.read(ref)["steps"]
        self.update(ref, owner, steps=dict(steps, **{name: dict(steps[name], result=result)}))

    def release(self, ref, owner):
        self.update(ref, owner, owner=None)
