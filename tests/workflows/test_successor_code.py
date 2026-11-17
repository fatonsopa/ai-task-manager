"""Fresh linked merge authority reuses actual owned candidate/check history."""

import secrets
from dataclasses import asdict, replace

import pytest

from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.application.merging.candidates import CandidateService, CoreCandidateAuthority
from ai_task_manager.application.merging.workflows import MergePlan, MergeWorkflowService
from ai_task_manager.application.workflows.recovery import SuccessorAuthorization
from ai_task_manager.domains.work.models import Evidence, SessionHandle
from tests.merge.workflows.helpers import Flow
from tests.merge.workflows.test_candidate_review import Captures
from tests.runtime.helpers import ProviderFixture


class RetainedCaptures(Captures):
    def resolve(self, inspection):
        value = super().resolve(inspection)
        self.saved = inspection.digest, value
        return value

    def restore(self, inspection):
        assert inspection.digest == self.saved[0]
        return self.saved[1]


@pytest.mark.parametrize("fence_cut", [False, True])
def test_expired_code_plan_continues_without_author_review_candidate_or_check_replay(
    tmp_path, fence_cut
):
    flow = Flow(tmp_path / "owned", human=False)
    try:
        source = RetainedCaptures(lambda: flow.r.now)
        files = ("src/a.txt", "tests/a.txt", "config/checks.json")
        reviews = CodeCandidateReviews(flow.r.core, flow.r.handle, flow.candidate, files, source)
        flow.workflow = flow.compose(candidate_reviews=reviews)

        def interruption(*args, **kwargs):
            raise RuntimeError("Fixture interruption before publication admission")

        flow.workflow.publisher_factory = interruption
        with pytest.raises(RuntimeError, match="before publication admission"):
            flow.workflow.run(flow.task.id, flow.plan.id, "original-merge")
        original_ref = next(iter(flow.workflow.references))
        original = flow.workflow.store.read(original_ref)
        assert original["steps"]["checks"]["result"]["outcome"] == "passed"
        assert "publication_intent" not in original["steps"]
        old_candidate = original["steps"]["candidate"]["result"]
        candidate_before = flow.candidate.store.get(old_candidate["operation_ref"])
        checks_before, reviews_before = len(flow.executor.records), len(source.calls)
        flow.r.now = flow.plan.deadline + 1
        task = flow.r.core.get_task(flow.r.handle, flow.task.id)
        decision = flow.r.proof(
            "decision",
            task.id,
            {"target": "In Progress", "revision": 0, "actor": "agent"},
            expires_at=flow.r.now + 150,
        )
        a = SuccessorAuthorization(
            flow.r.key(),
            flow.r.scope,
            "workflow:original",
            task.id,
            task.version,
            task.decision_revision,
            "fresh",
            "digest",
            "fresh-key",
            flow.r.now + 120,
            decision,
            (),
        )
        flow.r.proofs.evidence[a.reference] = Evidence(
            "workflow_successor", flow.r.scope, task.id, asdict(a), a.expires_at
        )
        routes = [
            route for route in flow.r.core.context.evidence.routes if route.prefix != "fixture:"
        ]
        flow.r.core.context.evidence = EvidenceRouter(
            [
                *routes,
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {
                            "decision",
                            "human_decision",
                            "provider_status",
                            "work_unit_complete",
                            "workflow_successor",
                        }
                    ),
                    flow.r.proofs,
                ),
            ]
        )
        handle = SessionHandle(secrets.token_urlsafe(32))
        flow.r.core.revoke_session(flow.r.handle)
        flow.r.core.admit_workflow_successor(flow.r.handle, handle, a, lambda *_: "workflow:fresh")
        from tests.scheduling.helpers import known_declaration

        known_declaration(flow.r, task, handle)
        flow.r.core.scheduling.successor_delivery(handle, task.id, flow.attempt.id)
        merge_decision = flow.r.proof(
            "decision",
            task.id,
            {"target": "In Dev", "revision": 0, "actor": "agent"},
            expires_at=a.expires_at,
        )
        grant = replace(flow.grant, task_version=task.version, decision_ref=merge_decision)
        plan = MergePlan("fresh-merge", grant, a.expires_at)
        candidate = CandidateService(
            flow.candidate.store.journal,
            flow.candidate_profile,
            CoreCandidateAuthority(flow.r.core, handle, flow.runtime.resources, flow.pair),
            process_factory=CandidateProcesses,
        )
        from ai_task_manager.domains.work.errors import AccessDenied

        with pytest.raises(AccessDenied):
            candidate.link_successor(
                old_candidate["operation_ref"], flow.grant, replace(grant, source_head="f" * 40)
            )
        candidate.link_successor(old_candidate["operation_ref"], flow.grant, grant)
        reviewed = CodeCandidateReviews(flow.r.core, handle, candidate, files, source)
        flow.check_inputs.select_retained = lambda _: None
        successor = MergeWorkflowService(
            flow.r.core,
            handle,
            plan,
            candidate,
            flow.check_inputs,
            ProviderFixture(flow.r),
            flow.evidence,
            clock=lambda: flow.r.now,
            candidate_reviews=reviewed,
            successor=(original_ref, a.reference),
        )
        if fence_cut:
            from tests.merge.f1_fixtures import FaultOnce

            successor.faults = FaultOnce("after_publication_fence")
            with pytest.raises(RuntimeError):
                successor.run(task.id, plan.id, "fresh-merge")
            current_ref = next(iter(successor.references))
            assert successor.store.read(current_ref)["steps"]["publication_fence"]["result"] is None
            result = successor.resume(current_ref)
        else:
            result = successor.run(task.id, plan.id, "fresh-merge")
        assert result["data"]["state"] == "done", result
        assert successor.store.read(original_ref) == original
        assert candidate.store.get(old_candidate["operation_ref"]) == candidate_before
        assert len(flow.executor.records) == checks_before == 1
        assert len(source.calls) == reviews_before == 1
        assert len(flow.runtime.runtime.workers) == 1
        assert flow.r.core.get_task(handle, task.id).observed_status == "In Dev"
        flow.workflow = successor
    finally:
        flow.close()


def test_partial_completed_code_review_uses_original_capture_under_fresh_authority(tmp_path):
    from types import SimpleNamespace

    from ai_task_manager.adapters.workflow_reviews.code_worker_artifacts import CodeWorkerArtifacts
    from ai_task_manager.adapters.workflow_reviews.code_worker_units import CodeWorkerUnitIssuer
    from ai_task_manager.domains.work.errors import EvidenceRequired
    from tests.workflow_reviews.helpers import ReviewRig

    rig = ReviewRig(tmp_path / "review", code=True)
    try:
        old = rig.lookup()["units"]["complete"]
        task = rig.complete(old)
        rig.r.now += 1000
        decision = rig.r.proof(
            "decision",
            task.id,
            {"target": "In Progress", "revision": 0, "actor": "agent"},
            expires_at=rig.r.now + 150,
        )
        a = SuccessorAuthorization(
            rig.r.key(),
            rig.scope,
            "workflow:old",
            task.id,
            task.version,
            0,
            "fresh",
            "digest",
            "fresh-key",
            rig.r.now + 120,
            decision,
            (),
        )
        rig.r.proofs.evidence[a.reference] = Evidence(
            "workflow_successor", rig.scope, task.id, asdict(a), a.expires_at
        )
        routes = [
            route for route in rig.r.core.context.evidence.routes if route.prefix != "fixture:"
        ]
        rig.r.core.context.evidence = EvidenceRouter(
            [
                *routes,
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {"decision", "provider_status", "human_decision", "workflow_successor"}
                    ),
                    rig.r.proofs,
                ),
            ]
        )
        handle = SessionHandle(secrets.token_urlsafe(32))
        rig.r.core.revoke_session(rig.r.handle)
        rig.r.core.admit_workflow_successor(rig.r.handle, handle, a, lambda *_: "workflow:fresh")
        fresh_plan = replace(rig.plan, digest="b" * 64)
        artifacts = CodeWorkerArtifacts(
            rig.r.core,
            handle,
            rig.store,
            rig.inspector,
            rig.view,
            {task.id: fresh_plan},
            dict(rig.artifacts.criteria),
        )
        source = SimpleNamespace(
            restore=rig.source.resolve,
            resolve=lambda *_: pytest.fail("Completed review must not invoke a producer"),
            check_binding=rig.source.check_binding,
        )
        issuer = CodeWorkerUnitIssuer(artifacts, rig.issuer_key, source, clock=lambda: rig.r.now)
        with pytest.raises(EvidenceRequired):
            issuer.lookup(
                rig.scope,
                task,
                rig.saved["digest"],
                retained=(rig.plan.digest, {"complete": "invented-proof"}, a.reference),
            )
        refs = issuer.lookup(
            rig.scope,
            task,
            rig.saved["digest"],
            retained=(rig.plan.digest, {"complete": old}, a.reference),
        )
        assert refs["units"]["complete"] == old
        assert issuer.verify_evidence(old).expires_at > rig.r.now
        assert rig.r.core.get_task(handle, task.id).completed_work == ("complete",)
    finally:
        rig.close()


def test_R03_ordinary_saved_fence_with_configured_review_resumes_before_git_effect(tmp_path):
    from tests.merge.f1_fixtures import FaultOnce

    flow = Flow(tmp_path / "ordinary")
    try:
        source = Captures(lambda: flow.r.now)
        review = CodeCandidateReviews(
            flow.r.core,
            flow.r.handle,
            flow.candidate,
            ("src/a.txt", "tests/a.txt", "config/checks.json"),
            source,
        )
        flow.check_inputs.select_retained = lambda _: None
        flow.workflow = flow.compose(
            candidate_reviews=review, faults=FaultOnce("after_publication_fence")
        )
        with pytest.raises(RuntimeError):
            flow.workflow.run(flow.task.id, flow.plan.id, "ordinary")
        ref = next(iter(flow.workflow.references))
        before = flow.workflow.store.read(ref)
        assert before["steps"]["publication_fence"]["result"] is None
        assert "publication" not in before["steps"]
        result = flow.workflow.resume(ref)
        assert result["data"]["state"] == "done", result
        assert len(flow.executor.records) == 1
        assert len(flow.runtime.runtime.workers) == 1
    finally:
        flow.close()
