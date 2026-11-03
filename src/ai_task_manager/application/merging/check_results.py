"""Translate complete persisted check evidence without choosing or executing checks."""

import time

from ai_task_manager.config.files import digest
from ai_task_manager.domains.checks.models import Scope
from ai_task_manager.domains.checks.reuse import eligible
from ai_task_manager.domains.merging.contracts import Observation, encode
from ai_task_manager.domains.work.errors import AccessDenied


class PersistedChecks:
    def __init__(self, checks, journal, grant):
        self.checks, self.journal, self.grant = checks, journal, grant
        self.last_verification = None

    def read(self, reference, binding):
        started = time.monotonic()
        if binding != self.grant.binding:
            raise AccessDenied("Check evidence belongs to a different publication")
        scope = Scope(binding.principal_id, binding.project_id, binding.session_id, binding.task_id)
        snapshot = self.checks.reads.read(
            reference, scope, after_wait=lambda: self.checks.catalogs.for_project(scope.project)
        )
        plan = snapshot["plan"]
        plan.record()
        self.checks.catalogs.verify(plan)
        self.checks.source_guard.verify(plan)
        request = self.grant.publication
        if (
            plan.scope != scope
            or plan.attempt != binding.attempt_id
            or plan.source_digest != binding.source_digest
            or (plan.base, plan.head) != (request.old, request.new)
            or request.checked != plan.head
            or snapshot["outcome"] != "passed"
            or set(snapshot["groups"]) != {g.key for g in plan.groups}
        ):
            raise AccessDenied("Check plan scope, input or outcome is not current")
        physical, carried, results = [], [], []
        for group in plan.groups:
            row = snapshot["groups"][group.key]
            result = row["result"]
            expected = {u.name for u in plan.units if u.group == group.key}
            if (
                row["state"] != "complete"
                or not result
                or result["outcome"] not in {"passed", "skipped"}
            ):
                raise AccessDenied("A required check result is incomplete")
            ran, reused = set(result["ran"]), result["carried"]
            if (
                set(result["selected"]) != expected
                or ran | set(reused) != expected
                or ran & set(reused)
                or set(result["passed"]) != ran
                or result["not_run"]
                or (expected and result["outcome"] != "passed")
            ):
                raise AccessDenied("Selected checks lack complete physical or reused evidence")
            if ran:
                if (
                    result.get("exit_code") != 0
                    or result.get("cause") != "completed"
                    or result.get("summary_available") is not True
                    or not result.get("process_ref")
                ):
                    raise AccessDenied("Physical check execution is unconfirmed")
                physical.append(result["ref"])
            for name, proof_ref in reused.items():
                unit = next(u for u in plan.units if u.name == name)
                proofs = self.checks.reads.proofs(
                    plan, unit, after_wait=lambda: self.checks.source_guard.verify(plan)
                )
                proof = next(
                    (p for p in proofs if p["ref"] == proof_ref and eligible(plan, unit, p)), None
                )
                if proof is None:
                    raise AccessDenied("Reused evidence lost its exact physical provenance")
                carried.append(proof["physical_origin"])
            results.append(result["ref"])
        operations = [
            op
            for op in self.journal.for_attempt(binding.attempt_id)
            if op["fingerprint"] == binding.fingerprint
        ]
        if len(operations) != 1:
            raise AccessDenied("Check observation has no unique registered publication")
        self.last_verification = {
            "plan": plan.digest,
            "results": results,
            "physical_origins": physical,
            "carried_origins": carried,
            "seconds": time.monotonic() - started,
        }
        return Observation(
            operations[0]["ref"],
            binding.fingerprint,
            reference,
            "checks",
            encode(
                {
                    "head": plan.head,
                    "permitted": True,
                    "contract_digest": digest(self.last_verification | {"seconds": None}),
                    "source_digest": binding.source_digest,
                }
            ),
        )
