"""Moving-target recovery uses actual default provisioning and retained worker work."""

import json

import pytest

from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.resources.git import git
from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.application.merging.workflows import (
    MergeEvidence,
    MergePlan,
    MergeWorkflowService,
)
from tests.code_provisioning.helpers import Provisioned
from tests.merge.workflows.test_candidate_review import Captures
from tests.runtime.helpers import ProviderFixture


def compose(owned):
    evidence = MergeEvidence(clock=lambda: owned.r.now)
    owned.r.core.context.evidence = EvidenceRouter(
        [
            *owned.r.core.context.evidence.routes,
            EvidenceRoute(
                "merge-publication:", frozenset({"merge", "publication_intent"}), evidence
            ),
        ]
    )
    reviews = CodeCandidateReviews(
        owned.r.core,
        owned.r.handle,
        owned.candidates,
        (owned.changed,) if hasattr(owned, "changed") else ("verify.py", "src/a.txt"),
        Captures(lambda: owned.r.now),
    )
    plan = MergePlan("measured-default", owned.grant, owned.context.deadline)
    return MergeWorkflowService(
        owned.r.core,
        owned.r.handle,
        plan,
        owned.candidates,
        owned.capability.checks,
        ProviderFixture(owned.r),
        evidence,
        clock=lambda: owned.r.now,
        candidate_reviews=reviews,
        maximum_attempts=owned.capability.maximum_attempts,
    )


def test_default_target_advance_recovers_without_worker_replay(tmp_path):
    owned = Provisioned(tmp_path / "owned")
    workflow = None
    try:
        manifest = json.loads(owned.manifest.read_text())
        manifest["attempts"] = 4
        from ai_task_manager.config.files import write_private

        write_private(owned.manifest, manifest, replace=True)
        owned.reseal()
        owned.prepare()
        original_worker = git(owned.pair.worktree, "rev-parse", "HEAD")
        target = git(
            owned.runtime.repo,
            "commit-tree",
            owned.base + "^{tree}",
            "-p",
            owned.base,
            "-m",
            "Independently authored target advance",
        )
        git(owned.runtime.repo, "update-ref", "refs/heads/integration", target, owned.base)
        workflow = compose(owned)
        result = workflow.run(owned.task.id, workflow.plan.id, "moving-target")
        (tmp_path / "moving-target-result.json").write_text(json.dumps(result, indent=2))
        assert result["data"]["state"] == "done", result
        assert git(owned.pair.worktree, "rev-parse", "HEAD") == original_worker
        assert git(owned.runtime.repo, "show", "integration:src/a.txt") == "4"
        assert len(owned.runtime.runtime.workers) == 1
    finally:
        if workflow is not None:
            workflow.close()
        owned.close()


@pytest.mark.parametrize("fault", ["attempts", "unknown", "hold", "deadline", "rewind"])
def test_target_successor_refuses_changed_or_unresolved_original_authority(tmp_path, fault):
    from ai_task_manager.config.files import write_private
    from ai_task_manager.domains.work.errors import WorkError

    owned = Provisioned(tmp_path / "owned")
    workflow = None
    try:
        manifest = json.loads(owned.manifest.read_text())
        manifest["attempts"] = 1 if fault == "attempts" else 4
        write_private(owned.manifest, manifest, replace=True)
        owned.reseal()
        owned.prepare()
        old_owner = owned.candidates.store.owner(owned.candidate.operation_ref)
        before = owned.pair.database.stat().st_ino
        target = git(
            owned.runtime.repo,
            "commit-tree",
            owned.base + "^{tree}",
            "-p",
            owned.base,
            "-m",
            "Authored target movement",
        )
        if fault == "rewind":
            target = git(owned.runtime.repo, "rev-parse", owned.base + "^")
        git(owned.runtime.repo, "update-ref", "refs/heads/integration", target, owned.base)
        if fault == "unknown":
            owned.candidates.store.effect(
                owned.candidate.operation_ref,
                "authored-unknown",
                {"scope": "unresolved child control"},
            )
        elif fault == "hold":
            owned.runtime.hold(owned.dispatch)
        elif fault == "deadline":
            owned.r.now = owned.context.deadline
        workflow = compose(owned)
        with pytest.raises(WorkError):
            workflow.run(owned.task.id, workflow.plan.id, "refused-recovery")
        assert owned.candidates.store.owner(owned.candidate.operation_ref) == old_owner
        assert owned.pair.database.stat().st_ino == before
        assert git(owned.pair.worktree, "rev-parse", "HEAD") == owned.grant.source_head
        assert not owned.capability.checks.checks.source_guard.runner.jobs
        assert git(owned.runtime.repo, "rev-parse", "refs/heads/integration") == target
    finally:
        if workflow is not None:
            workflow.close()
        owned.close()


def test_claim_transfer_interruption_resumes_exact_pending_successor(tmp_path, monkeypatch):
    from ai_task_manager.config.files import write_private

    owned = Provisioned(tmp_path / "owned")
    workflow = None
    try:
        manifest = json.loads(owned.manifest.read_text())
        manifest["attempts"] = 4
        write_private(owned.manifest, manifest, replace=True)
        owned.reseal()
        owned.prepare()
        target = git(
            owned.runtime.repo,
            "commit-tree",
            owned.base + "^{tree}",
            "-p",
            owned.base,
            "-m",
            "Authored forward target",
        )
        git(owned.runtime.repo, "update-ref", "refs/heads/integration", target, owned.base)
        workflow = compose(owned)
        journal = owned.candidates.store.journal
        original = journal.commit
        failed = []

        def interrupted(ref, expected, key, kind, *args, **kwargs):
            if kind == "candidate_intent" and not failed:
                failed.append(ref)
                raise RuntimeError("Authored crash after canonical candidate claim transfer")
            return original(ref, expected, key, kind, *args, **kwargs)

        monkeypatch.setattr(journal, "commit", interrupted)
        with pytest.raises(RuntimeError, match="claim transfer"):
            workflow.run(owned.task.id, workflow.plan.id, "pending-successor")
        assert len(failed) == 1
        pending = journal.current(failed[0])
        assert pending["facts"] == {}
        predecessor = owned.candidates.store.get(owned.candidate.operation_ref)
        claim = journal.current(predecessor["claim"])
        assert claim["facts"]["candidate_owner"]["operation"] == failed[0]
        assert len(claim["facts"]["candidate_owner_history"]) == 1
        reference = next(iter(workflow.references))
        before = workflow.store.read(reference)
        monkeypatch.setattr(journal, "commit", original)
        result = workflow.resume(reference)
        assert result["data"]["state"] == "done", result
        after = workflow.store.read(reference)
        assert after["request"] == before["request"]
        assert after["target_lineage"] == before["target_lineage"]
        assert after["steps"]["candidate"]["result"]["operation_ref"] == failed[0]
        assert owned.pair.worktree.exists() and owned.pair.database.exists()
        assert len(owned.runtime.runtime.workers) == 1
    finally:
        if workflow is not None:
            workflow.close()
        owned.close()
