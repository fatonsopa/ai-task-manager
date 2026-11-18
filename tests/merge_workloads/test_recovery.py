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


def test_published_member_milestone_refreshes_after_advance_expiry_and_reconstruction(tmp_path):
    from dataclasses import asdict
    from types import SimpleNamespace

    from ai_task_manager.domains.scheduling.models import Dependency
    from ai_task_manager.domains.work.errors import EvidenceRequired
    from ai_task_manager.installation.workflows.member_evidence import MemberMilestones
    from tests.merge_workloads.fixture import Owner, Project
    from tests.scheduling.helpers import declare

    project = Project(tmp_path / "milestone")
    a, b = Owner(project, 0), Owner(project, 1)
    observations = {
        "scope": (
            "Actual Git/SQL/default merge producer and Core; authored reviews, provider and "
            "member adapter. Component reconstruction is not an OS owner restart."
        )
    }
    try:
        a.workflow = compose(a)
        first = a.workflow.run(a.task.id, a.workflow.plan.id, "milestone-a")
        assert first["data"]["state"] == "done", first
        reference = next(
            ref for ref, (value, _) in a.workflow.evidence.prepared.items() if value.kind == "merge"
        )
        delivery = SimpleNamespace(
            evidence=a.workflow.evidence,
            prepare_merge_milestone=lambda ref, plan: a.workflow.prepare_merge_milestone(
                first["data"]["workflow"], ref
            ),
        )
        member = SimpleNamespace(
            spec=SimpleNamespace(
                data={
                    "scope": asdict(a.r.scope),
                    "task_id": a.task.id,
                    "plan_ref": a.workflow.plan.id,
                },
                check=lambda: None,
            ),
            review_issuer=None,
            service=SimpleNamespace(
                workflows=SimpleNamespace(
                    driver=SimpleNamespace(
                        code_delivery=delivery, plans=SimpleNamespace(get=lambda ref: None)
                    )
                )
            ),
        )
        router = MemberMilestones(a.r.core.context.evidence.routes, (member,))
        a.r.core.context.evidence = router
        task = a.r.queued(code=False)
        edge = Dependency(a.task.id, "code_merged", evidence_refs=(("merge", reference),))
        assert (
            declare(a.r.core, a.r.handle, task, a.r.scheduling_sources, a.r.now, (edge,))["state"]
            == "ready"
        )
        original_target = git(project.repo, "rev-parse", "integration")
        b.workflow = compose(b)
        second = b.workflow.run(b.task.id, b.workflow.plan.id, "milestone-b")
        assert second["data"]["state"] == "done", second
        advanced_target = git(project.repo, "rev-parse", "integration")
        assert original_target != advanced_target
        git(project.repo, "merge-base", "--is-ancestor", original_target, advanced_target)
        assert a.r.core.scheduling.explain(a.r.handle, task.id)["state"] == "blocked"
        assert router.prepare_merge_milestone(a.task.id, reference) == reference
        assert a.r.core.scheduling.explain(a.r.handle, task.id)["state"] == "ready"
        expiry = a.workflow.evidence.prepared[reference][0].expires_at
        a.r.now = expiry
        assert a.r.core.scheduling.explain(a.r.handle, task.id)["state"] == "blocked"
        assert router.prepare_merge_milestone(a.task.id, reference) == reference
        assert a.r.core.scheduling.explain(a.r.handle, task.id)["state"] == "ready"
        refreshed_expiry = a.workflow.evidence.prepared[reference][0].expires_at
        assert refreshed_expiry == a.r.now + 5
        jobs = len(a.capability.checks.checks.source_guard.runner.jobs)
        a.workflow.close()
        a.r.core.context.evidence = EvidenceRouter(
            [r for r in a.r.core.context.evidence.routes if r.prefix != "merge-publication:"]
        )
        a.workflow = compose(a)
        assert not a.workflow.evidence.prepared
        delivery.evidence = a.workflow.evidence
        router = MemberMilestones(a.r.core.context.evidence.routes, (member,))
        a.r.core.context.evidence = router
        assert router.prepare_merge_milestone(a.task.id, reference) == reference
        assert a.r.core.scheduling.explain(a.r.handle, task.id)["state"] == "ready"
        assert len(a.runtime.runtime.workers) == len(b.runtime.runtime.workers) == 1
        assert len(a.capability.checks.checks.source_guard.runner.jobs) == jobs
        assert git(project.repo, "rev-parse", "integration") == advanced_target
        # Rebuild the installed producer through its real existing component and
        # scoped-head consumers. Runtime lookup/semantic ports remain authored.
        from ai_task_manager.application.workflows.models import WorkflowPlan
        from ai_task_manager.installation.workflows.code_delivery import InstalledCodeDelivery
        from ai_task_manager.storage.workflows import WorkflowStore

        outer = WorkflowStore(tmp_path / "outer.sqlite")
        current_task = a.r.core.get_task(a.r.handle, a.task.id)
        plan = WorkflowPlan(
            "retained-outer",
            "assignment",
            "fixture",
            project.base,
            (a.changed,),
            (a.changed,),
            a.context.deadline,
        )
        run_ref = outer.create(a.r.scope, current_task, plan, "original-outer")
        claim = "owned-history-control"
        outer.claim(run_ref, a.r.scope, claim)
        merge_store = a.candidates.store.journal
        check_store = a.capability.checks.checks.store
        history = {
            "merge": {
                "path": str(merge_store.path),
                "device": merge_store.identity[0],
                "inode": merge_store.identity[1],
                "store_id": merge_store.store_id,
            },
            "checks": {
                "path": str(check_store.path),
                "device": check_store.file_identity[0],
                "inode": check_store.file_identity[1],
                "store_id": check_store.store_id,
            },
        }
        commit = {
            "commit": a.grant.source_head,
            "branch": a.grant.source_ref.removeprefix("refs/heads/"),
        }
        for name, payload, result in [
            ("worker-commit", {}, commit),
            (
                "code-plan",
                {"plan": asdict(a.workflow.original_plan), "history": history},
                {"installed": True},
            ),
            ("code-merge", {}, {"state": "done", "merge_workflow": first["data"]["workflow"]}),
        ]:
            outer.intend(run_ref, claim, name, payload)
            outer.finish(run_ref, claim, name, result, attempt_id=a.attempt.id)
        outer.release(run_ref, claim)
        native_delivery = InstalledCodeDelivery(
            a.r.core,
            a.r.handle,
            outer,
            SimpleNamespace(pair=lambda attempt: a.pair),
            a.runtime.resources,
            a.code,
            a.context,
            Captures(lambda: a.r.now),
            tmp_path / "units.key",
            lambda *args: None,
            ProviderFixture(a.r),
            ("complete",),
        )
        assert not native_delivery.components and not native_delivery.evidence.prepared
        delivery.evidence = native_delivery.evidence
        delivery.prepare_merge_milestone = native_delivery.prepare_merge_milestone
        member.service.workflows.driver.plans = SimpleNamespace(get=lambda ref: plan)
        assert router.prepare_merge_milestone(a.task.id, reference) == reference
        assert a.r.core.scheduling.explain(a.r.handle, task.id)["state"] == "ready"
        assert len(native_delivery.components) == 1
        with pytest.raises(EvidenceRequired, match="original installed task"):
            router.prepare_merge_milestone("foreign-task", reference)
        with pytest.raises(EvidenceRequired, match="reference changed"):
            router.prepare_merge_milestone(a.task.id, "merge-publication:foreign")
        git(project.repo, "update-ref", "refs/heads/integration", project.base, advanced_target)
        from ai_task_manager.domains.work.errors import WorkError

        with pytest.raises(WorkError, match="ancestry|source or target changed"):
            router.prepare_merge_milestone(a.task.id, reference)
        observations.update(
            original_target=original_target,
            advanced_target=advanced_target,
            original_ref=reference,
            original_expiry=expiry,
            fixture_verifier_clock=a.r.now,
            refreshed_expiry=refreshed_expiry,
            worker_counts=[1, 1],
            unchanged_check_jobs=jobs,
        )
    finally:
        a.close()
        b.close()
        (tmp_path / "milestone-observations.json").write_text(json.dumps(observations, indent=2))
