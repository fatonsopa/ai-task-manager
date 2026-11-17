"""Closed proof routing over real issuers; captures/merge observations are authored."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.workflow_reviews.code_worker_units import CodeWorkerUnitIssuer
from ai_task_manager.adapters.workflow_reviews.models import FileWitness
from ai_task_manager.application.merging.workflows.evidence import MergeEvidence
from ai_task_manager.domains.scheduling.models import Dependency
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Cause, Evidence, IssueStatus
from ai_task_manager.installation.workflows.member_evidence import MemberMilestones
from tests.scheduling.helpers import declare
from tests.workflow_reviews.helpers import ReviewRig


def member(rig, *, core=None, code=False):
    delivery = SimpleNamespace(issuer=rig.issuer, evidence=MergeEvidence(clock=lambda: rig.r.now)) if code else None
    return SimpleNamespace(
        spec=SimpleNamespace(data={"scope": asdict(rig.scope), "task_id": rig.task.id}, check=lambda: None),
        review_issuer=None if code else rig.issuer,
        service=SimpleNamespace(workflows=SimpleNamespace(driver=SimpleNamespace(code_delivery=delivery)),
                                core=core or rig.r.core),
    )


@pytest.mark.parametrize("code", [False, True])
def test_current_member_issuer_releases_explicit_milestone_without_foreign_authority(tmp_path, code):
    a = ReviewRig(tmp_path / "a", code=code)
    b = ReviewRig(tmp_path / "b", code=code)
    try:
        references = a.lookup()
        reference = references["units"]["complete"]
        a.task = a.complete(reference)
        dependent = a.r.queued(code=False)
        # A separate calling Core has the same canonical task DB/session and its
        # own proof router, matching installed member composition.
        other_core = a.r.new_core()
        other_core.context.evidence = b.r.core.context.evidence
        edge = Dependency(a.task.id, "unit_verified", "complete", (("work_unit_complete", reference),))
        before = declare(other_core, a.r.handle, dependent, a.r.scheduling_sources, a.r.now, (edge,))
        assert before["state"] == "blocked"
        nodes = (member(a, code=code), member(b, core=other_core, code=code))
        router = MemberMilestones(other_core.context.evidence.routes, nodes)
        other_core.context.evidence = router
        a.r.core.context.evidence = MemberMilestones(a.r.core.context.evidence.routes, nodes)
        assert router.verify_evidence(reference).subject == a.task.id
        explained = other_core.scheduling.explain(a.r.handle, dependent.id)
        assert explained["state"] == "ready", explained
        if not code:
            assert router.verify_evidence(references["delivery"]).kind == "non_code_completion"
        with pytest.raises(EvidenceRequired):
            router.verify_evidence(("code-worker-review:" if code else "workflow-review:") + "foreign")
        wrong = member(a, code=code)
        wrong.spec.data["task_id"] = "different-subject"
        with pytest.raises(EvidenceRequired, match="original installed task and scope"):
            MemberMilestones(router.routes, (wrong, nodes[1])).verify_evidence(reference)
        wrong.spec.data["task_id"] = a.task.id
        wrong.spec.data["scope"] = asdict(replace(a.scope, principal="foreign"))
        with pytest.raises(EvidenceRequired, match="original installed task and scope"):
            MemberMilestones(router.routes, (wrong, nodes[1])).verify_evidence(reference)

        if code:
            fresh = CodeWorkerUnitIssuer(a.artifacts, a.issuer_key, a.source, clock=lambda: a.r.now)
            nodes[0].service.workflows.driver.code_delivery.issuer = fresh
            with pytest.raises(EvidenceRequired):
                router.verify_evidence(reference)
            assert fresh.lookup(a.scope, a.task, a.saved["digest"]) == references
            assert router.verify_evidence(reference).subject == a.task.id
            assert isinstance(router.with_routes(router.routes), MemberMilestones)
            # Reuse the accepted merge producer contract. This routing control
            # supplies an authored prepared observation with a real file witness;
            # it performs no merge, container work or model acceptance.
            merge = nodes[0].service.workflows.driver.code_delivery.evidence
            merge_reference = "merge-publication:owned-routing-control"
            witness = FileWitness.capture(a.report)
            merge.prepared[merge_reference] = (
                Evidence("merge", a.scope, a.task.id,
                         {"commit": "a" * 40, "prose_digest": a.task.prose_digest}, a.r.now + 5),
                (witness,),
            )
            assert router.verify_evidence(merge_reference).kind == "merge"
            a.task = a.r.transition(a.task, IssueStatus.IN_DEV, Cause.MERGE, {"merge": merge_reference})
            dependent = other_core.get_task(a.r.handle, dependent.id)
            merged_edge = Dependency(a.task.id, "code_merged", evidence_refs=(("merge", merge_reference),))
            assert declare(other_core, a.r.handle, dependent, a.r.scheduling_sources,
                           a.r.now, (merged_edge,))["state"] == "ready"
            nodes[0].spec.data["task_id"] = "foreign"
            with pytest.raises(EvidenceRequired):
                router.verify_evidence(merge_reference)
            nodes[0].spec.data["task_id"] = a.task.id
            a.report.write_text("changed after prepared observation")
            with pytest.raises(EvidenceRequired):
                router.verify_evidence(merge_reference)
        else:
            a.task = a.r.transition(a.task, IssueStatus.DONE, Cause.NON_CODE_COMPLETE,
                                    {"non_code_completion": references["delivery"]})
            dependent = other_core.get_task(a.r.handle, dependent.id)
            done_edge = Dependency(a.task.id, "work_done",
                                   evidence_refs=(("non_code_completion", references["delivery"]),))
            assert declare(other_core, a.r.handle, dependent, a.r.scheduling_sources,
                           a.r.now, (done_edge,))["state"] == "ready"
            a.r.now += 6
            with pytest.raises(EvidenceRequired):
                router.verify_evidence(reference)
    finally:
        a.close()
        b.close()
