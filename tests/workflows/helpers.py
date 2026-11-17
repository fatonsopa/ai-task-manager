"""Authored local composition; no live account, model or semantic-review acceptance."""

import os
import subprocess
import sys
from dataclasses import replace

from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
from ai_task_manager.adapters.workflows.repository import RepositoryInspector
from ai_task_manager.adapters.workflows.runtime import LocalCommands, LocalSupervisorPort
from ai_task_manager.application.workflows.driver import WorkflowDriver
from ai_task_manager.application.workflows.models import WorkflowPlan, WorkflowPlans
from ai_task_manager.application.workflows.reviews import ReviewedEvidence
from ai_task_manager.storage.workflows import WorkflowStore
from tests.runtime.helpers import ProviderFixture, RuntimeRig


class WorkflowFixture:
    def __init__(self, path, *, program=None, max_steps=16, resource_locks=()):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.rr = RuntimeRig(path)
        self.rig = r = self.rr.rig
        self.task = r.queued(code=False)
        self.commands = LocalCommands(
            {
                "write-result": (
                    (
                        sys.executable,
                        "-I",
                        "-c",
                        program
                        or (
                            "from pathlib import Path;"
                            "Path('result.txt').write_text('Observed local result\\n')"
                        ),
                    ),
                    {"PATH": os.defpath},
                    2,
                )
            }
        )
        self.runtime = LocalSupervisorPort(self.rr.supervisor, self.commands)
        self.store = WorkflowStore(path / "workflow.sqlite")
        base = subprocess.check_output(["git", "-C", str(self.rr.repo), "rev-parse", "HEAD"])
        self.plan = WorkflowPlan(
            "non-code",
            "write-result",
            "fixture",
            base.decode().strip(),
            ("result.txt",),
            ("result.txt",),
            r.now + 120,
            max_steps,
            resource_locks=resource_locks,
        )
        self.plans = WorkflowPlans((self.plan,))
        self.driver = self.new_driver()
        self.ref = self.driver.begin(self.task.id, self.plan.id, "workflow-intake")

    def new_driver(self, **kwargs):
        return WorkflowDriver(
            self.rig.core,
            self.rig.handle,
            self.store,
            self.plans,
            runtime=self.runtime,
            repository=RepositoryInspector(self.runtime),
            provider=ProviderFixture(self.rig),
            decisions=lambda scope, task, target, action: self.rig.decision(task, target),
            **kwargs,
        )

    def authored_reviews(self):
        r = self.rig
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, self.rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {
                            "provider_status",
                            "decision",
                            "human_decision",
                            "work_unit_complete",
                            "non_code_completion",
                        }
                    ),
                    r.proofs,
                ),
            ]
        )

        def lookup(scope, task, digest):
            payload = {
                "prose_digest": task.prose_digest,
                "artifact_digest": digest,
                "artifact": digest,
            }
            return {
                "units": {
                    u: r.proof("work_unit_complete", task.id, dict(payload, unit=u))
                    for u in task.required_work
                },
                "delivery": r.proof("non_code_completion", task.id, payload),
            }

        return ReviewedEvidence(r.core, lookup)

    def changed_plan(self, **kwargs):
        self.plans = WorkflowPlans((replace(self.plan, **kwargs),))
        return self.new_driver()

    def close(self):
        import json
        from pathlib import Path

        self.rr.close()
        reports = []
        for worker in self.rr.runtime.workers.values():
            observation = worker.group.observe()
            inventory = subprocess.check_output(
                ["ps", "-axo", "pid=,pgid=,stat=,lstart="], text=True
            )
            remaining = [
                line
                for line in inventory.splitlines()
                if len(line.split()) >= 3
                and int(line.split()[1]) == worker.process.pid
                and not line.split()[2].startswith(("Z", "X"))
            ]
            outcomes = self.rr.journal.observations(worker.attempt_id, "runtime_outcome")
            reports.append(
                {
                    "pid": worker.process.pid,
                    "group": worker.process.pid,
                    "start_identity": worker.group.start_identity,
                    "returncode": worker.process.poll(),
                    "observation_after_close": observation.record(),
                    "actual_outcomes": outcomes,
                    "remaining_live_group_rows": remaining,
                }
            )
            assert outcomes and outcomes[-1]["quiescent"] and not remaining
            assert worker.process.returncode is not None
        output = os.environ.get("WORKFLOW_RESOURCE_REPORT")
        if output:
            with Path(output).open("a") as stream:
                stream.write(json.dumps({"fixture": str(self.rr.path), "workers": reports}) + "\n")
