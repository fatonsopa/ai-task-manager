"""Actual local Core/runtime/files, with explicitly authored review observations."""

import hashlib
import hmac
import json
from dataclasses import dataclass

from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
from ai_task_manager.adapters.workflow_reviews import (
    CurrentArtifacts,
    InstalledReviews,
    ReviewBinding,
    WorkflowReviews,
)
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.checks.models import canonical, fingerprint
from tests.runtime.helpers import RuntimeRig


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Plan:
    base: str
    artifacts: tuple = ("report.txt",)
    allowed_files: tuple = ("report.txt",)
    digest: str = "a" * 64


class RuntimeView:
    def __init__(self, runtime, dispatch):
        self.runtime, self.dispatch = runtime, dispatch

    def pair(self, attempt_id):
        assert attempt_id == self.dispatch.attempt_id
        self.runtime.resources.verify(self.dispatch.resources)
        return self.dispatch.resources

    def ownership(self, attempt_id):
        pair = self.pair(attempt_id)
        original = self.runtime.resources.verify(pair)
        return self.runtime.resources.git.inspect(pair.worktree, original["worktree"]["branch"])


class InspectionContractControl:
    """Authored contract-shaped inspection, not actual WorkflowStore/controller integration."""

    def __init__(self, runtime):
        self.runtime = runtime

    def inspect(self, task, attempt, plan):
        pair = self.runtime.pair(attempt.id)
        tree = self.runtime.ownership(attempt.id)
        artifacts, facts = {}, {}
        for name in plan.artifacts:
            path = pair.worktree / name
            info, raw = path.stat(), path.read_bytes()
            artifacts[name] = sha(raw)
            facts[name] = {
                "path": str(path),
                "device": info.st_dev,
                "inode": info.st_ino,
                "size": len(raw),
                "sha256": sha(raw),
            }
        body = {
            "task_id": task.id,
            "attempt_id": attempt.id,
            "prose_digest": task.prose_digest,
            "files": list(plan.artifacts),
            "artifacts": artifacts,
            "artifact_facts": facts,
            "source_head": tree["head"],
            "worktree": tree,
            "resource_ids": [pair.worktree_id, pair.database_id],
        }
        return dict(body, digest=fingerprint(body))


class HistoricalControl:
    def __init__(self, path, scope, observation):
        self.path, self.scope, self.calls = path, scope, 0
        write_private(path, observation)

    def verified(self, scope, task_id, artifact_digest):
        self.calls += 1
        value = json.loads(self.path.read_text())
        return (
            value
            if (
                scope == self.scope
                and value["task_id"] == task_id
                and value["digest"] == artifact_digest
            )
            else None
        )


class ReviewRig:
    def __init__(self, root, *, code=False):
        root.mkdir(mode=0o700)
        self.root = root
        self.runtime = RuntimeRig(root)
        self.r = self.runtime.rig
        task = self.r.queued(code=code)
        self.dispatch = self.runtime.start(task)
        self.runtime.admit(self.dispatch)
        command = self.runtime.command(
            self.dispatch,
            "from pathlib import Path; "
            "Path('report.txt').write_text('Authored report artifact.\\n')"
            + (
                "; import subprocess; subprocess.run(['git','add','report.txt'],check=True); "
                "subprocess.run(['git','commit','-qm','Owned worker artifact'],check=True)"
                if code
                else ""
            ),
        )
        self.runtime.supervisor.execute(
            self.r.handle, self.dispatch, command, operation_key=self.r.key()
        )
        self.task = self.r.core.get_task(self.r.handle, task.id)
        self.attempt = self.r.core.get_attempt(self.r.handle, self.dispatch.attempt_id)
        assert self.attempt.state == "done"
        self.scope = self.r.scope
        self.view = RuntimeView(self.runtime, self.dispatch)
        self.plan = Plan(self.view.ownership(self.attempt.id)["head"])
        self.inspector = InspectionContractControl(self.view)
        self.saved = self.inspector.inspect(self.task, self.attempt, self.plan)
        self.store = HistoricalControl(root / "inspection.json", self.scope, self.saved)
        from ai_task_manager.adapters.workflow_reviews.code_worker_artifacts import (
            CodeWorkerArtifacts,
        )

        self.code = code
        artifact_type = CodeWorkerArtifacts if code else CurrentArtifacts
        self.artifacts = artifact_type(
            self.r.core,
            self.r.handle,
            self.store,
            self.inspector,
            self.view,
            {self.task.id: self.plan},
            {self.task.id: (("request", "The artifact addresses the declared request"),)},
        )
        self.issuer_key, self.capture_key = root / "issuer.key", root / "capture.key"
        for path in (self.issuer_key, self.capture_key):
            path.write_bytes(b"x" * 32)
            path.chmod(0o600)
        self.revoked = root / "revoked.json"
        write_private(self.revoked, {"revoked": []})
        self.install()

    @property
    def report(self):
        return self.dispatch.resources.worktree / "report.txt"

    def install(self, *, judgment="accept", changes=None, suffix="1", prior=()):
        self.inspection = self.artifacts.prepare(self.scope, self.task, self.saved["digest"])
        trace = self.root / ("review-" + suffix + ".trace")
        trace.write_text("Authored review control. No real model or human judgment is claimed.\n")
        trace.chmod(0o600)
        record = {
            "reference": "control-review:" + suffix,
            "context_digest": self.inspection.digest,
            "operation_key": "control-operation:" + suffix,
            "reviewer_id": "authored-control",
            "host": "authored-control",
            "model": None,
            "provenance_ref": "authored-control:capture",
            "trace_sha256": sha(trace.read_bytes()),
            "criteria": [
                {
                    "id": "request",
                    "judgment": judgment,
                    "reason": "Authored criterion observation for adapter testing",
                }
            ],
            "reason": "Authored control, not real semantic acceptance",
            "expires_at": self.r.now + 60,
        }
        record.update(changes or {})
        capture = self.root / ("review-" + suffix + ".json")
        signature = hmac.new(
            self.capture_key.read_bytes(), canonical(record).encode(), hashlib.sha256
        ).hexdigest()
        write_private(capture, {"record": record, "signature": signature})
        self.binding = ReviewBinding(
            "control-review:" + suffix,
            self.inspection.digest,
            self.scope,
            self.task.id,
            "control-operation:" + suffix,
            capture,
            sha(capture.read_bytes()),
            trace,
            sha(trace.read_bytes()),
            "authored-control",
            "authored-control",
            None,
            "authored-control:capture",
        )
        self.source = InstalledReviews(
            self.capture_key,
            self.revoked,
            tuple(prior) + (self.binding,),
            selected={self.inspection.digest: self.binding.reference},
        )
        from ai_task_manager.adapters.workflow_reviews.code_worker_units import (
            CodeWorkerUnitIssuer,
        )

        issuer_type = CodeWorkerUnitIssuer if self.code else WorkflowReviews
        self.issuer = issuer_type(
            self.artifacts, self.issuer_key, self.source, clock=lambda: self.r.now
        )
        self.r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute(self.issuer.prefix, self.issuer.kinds, self.issuer),
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, self.runtime.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset({"provider_status", "decision", "human_decision"}),
                    self.r.proofs,
                ),
            ]
        )
        return self.binding

    def lookup(self):
        task = self.r.core.get_task(self.r.handle, self.task.id)
        return self.issuer.lookup(self.scope, task, self.saved["digest"])

    def complete(self, ref, *, key=None, task=None):
        task = task or self.r.core.get_task(self.r.handle, self.task.id)
        return self.r.core.execution.complete_unit(
            self.r.handle,
            task.id,
            "complete",
            task.version,
            task.decision_revision,
            ref,
            key or self.r.key(),
        )

    def close(self):
        self.runtime.close()
        state = {
            "all_workers_stopped": all(
                w.process.returncode is not None for w in self.runtime.runtime.workers.values()
            ),
            "fixture_review_provenance": "authored control; not real model/human acceptance",
        }
        (self.root / "review-cleanup.json").write_text(json.dumps(state))
        assert state["all_workers_stopped"]
