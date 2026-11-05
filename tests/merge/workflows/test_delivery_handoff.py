"""Actual local publication proves exact collector-to-canonical-fence handoff."""

from dataclasses import replace

import pytest

from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.application.publication_fences import PublicationContext
from ai_task_manager.domains.work.errors import AccessDenied, WorkError
from ai_task_manager.installation.workflows.code_delivery import DeliveryResources
from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.merge.workflows.helpers import Flow
from tests.merge.workflows.test_candidate_review import Captures
from tests.merge.workflows.test_workflow import FaultOnce
from tests.scheduling.helpers import snapshot


@pytest.fixture
def owned(tmp_path):
    flow = Flow(tmp_path / "owned")
    source = AuthoredDependencyCollection(flow.r.core, flow.r.handle)
    lifecycle = DeliveryDependencies(flow.r.core, flow.r.handle, source, flow.plan.deadline)
    lifecycle.bind(
        {
            "task_id": flow.task.id,
            "attempt_id": flow.attempt.id,
            "prose_digest": flow.task.prose_digest,
            "decision_revision": flow.task.decision_revision,
        }
    )
    resources = DeliveryResources(
        flow.runtime.resources, lambda: flow.r.now, flow.plan.deadline, lifecycle
    )
    flow.candidate.authority.resources = resources
    reviews = CodeCandidateReviews(
        flow.r.core,
        flow.r.handle,
        flow.candidate,
        ("src/a.txt", "tests/a.txt", "config/checks.json"),
        Captures(lambda: flow.r.now),
    )
    flow.workflow = flow.compose(candidate_reviews=reviews, publication_lifecycle=lifecycle)
    flow.app.workflows = flow.workflow
    try:
        yield flow, lifecycle
    finally:
        lifecycle.close()
        assert lifecycle._thread is None or not lifecycle._thread.is_alive()
        flow.close()


def test_normal_publication_handoff_and_canonical_committed_inspection(owned):
    flow, lifecycle = owned
    result = flow.run()
    assert result["data"]["state"] == "done", result
    ref = result["data"]["workflow"]
    row = flow.workflow.store.read(ref)
    context = PublicationContext(**row["steps"]["publication_fence"]["result"])
    fact = flow.r.core.publications.inspect(flow.r.handle, flow.task.id, context)
    assert fact["state"] == "committed"
    fact["intent"]["task_id"] = "mutated-local-copy"
    assert (
        flow.r.core.publications.inspect(flow.r.handle, flow.task.id, context)["intent"]["task_id"]
        == flow.task.id
    )
    assert lifecycle._thread is None or not lifecycle._thread.is_alive()
    before = flow.r.core.notifications.list_events(flow.r.handle)
    flow.r.now += 31
    g = flow.grant
    assert (
        flow.r.core.publications.begin(
            flow.r.handle,
            flow.task.id,
            flow.attempt.id,
            g.task_version,
            g.attempt_version,
            g.decision_revision,
            g.decision_ref,
            row["steps"]["publication_intent"]["result"]["ref"],
            ref + ":fence",
        )
        == context
    )
    assert flow.resume(ref)["data"]["state"] == "done"
    assert flow.r.core.notifications.list_events(flow.r.handle) == before
    with pytest.raises(WorkError):
        with flow.r.core.execution.merge_snapshot_boundary(
            flow.r.handle,
            flow.task.id,
            flow.attempt.id,
            flow.grant.task_version,
            flow.grant.attempt_version,
            flow.grant.decision_revision,
            flow.grant.decision_ref,
            publication_context=context,
        ):
            pytest.fail("Committed inspection cannot authorize another effect")
    with pytest.raises(WorkError):
        flow.r.core.publications.inspect(
            flow.r.handle, flow.task.id, replace(context, binding_digest="0" * 64)
        )
    foreign = flow.r.session(replace(flow.r.scope, logical_session="foreign"))
    with pytest.raises(WorkError):
        flow.r.core.publications.inspect(foreign, flow.task.id, context)


@pytest.mark.parametrize("case", ["expired", "invalidated", "foreign", "failed_observer"])
def test_new_publication_refuses_without_current_original_graph(owned, case):
    flow, lifecycle = owned
    flow.workflow.faults = FaultOnce("after_publication_intent")
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    intent = next(
        reference
        for reference, (fact, _) in flow.evidence.prepared.items()
        if fact.kind == "publication_intent"
    )
    if case == "expired":
        flow.r.now += 31
    elif case == "invalidated":
        with flow.r.db.transaction() as conn:
            conn.execute(
                "UPDATE scheduling_attempts SET invalidated_by='authored-invalidated' "
                "WHERE attempt_id=?",
                (flow.attempt.id,),
            )
    elif case == "foreign":
        value = snapshot(flow.r.core, flow.r.handle, flow.task, flow.r.now)
        value = replace(value, scope=replace(flow.r.scope, principal="foreign"))
        reference = flow.r.scheduling_sources.issue(value)
        with pytest.raises(WorkError):
            flow.r.core.scheduling.observe(
                flow.r.handle,
                flow.task.id,
                flow.task.version,
                flow.task.decision_revision,
                reference,
                "foreign-graph",
            )
        with pytest.raises(WorkError):
            lifecycle.adopt_fence(
                flow.plan, PublicationContext("fabricated", "0" * 64, flow.new), "0" * 64, flow.new
            )
        return
    else:
        lifecycle._error = AccessDenied("Authored unavailable current source")
    flow.workflow.faults = None
    if case == "failed_observer":
        assert not flow.resume(ref)["ok"]
    else:
        g = flow.grant
        with pytest.raises(WorkError):
            flow.r.core.publications.begin(
                flow.r.handle,
                flow.task.id,
                flow.attempt.id,
                g.task_version,
                g.attempt_version,
                g.decision_revision,
                g.decision_ref,
                intent,
                "new-fence-without-current-graph",
            )
    with flow.r.db.transaction() as conn:
        from ai_task_manager.storage import issue_claims, merging_fences

        assert not merging_fences.pending(
            conn, issue_claims.identity(flow.r.scope, flow.task.issue_id)
        )


@pytest.mark.parametrize("cut", ["after_publication_fence", "after_publication"])
def test_saved_exact_publication_context_resumes_without_generic_authority(owned, cut):
    flow, lifecycle = owned
    flow.workflow.faults = FaultOnce(cut)
    assert not flow.run()["ok"]
    ref = next(iter(flow.workflow.references))
    row = flow.workflow.store.read(ref)
    candidate = row["steps"]["candidate"]["result"]
    flow.r.now += 31
    flow.workflow.faults = None
    result = flow.resume(ref)
    assert result["data"]["state"] == "done", result
    assert flow.workflow.store.read(ref)["steps"]["candidate"]["result"] == candidate
    assert lifecycle._thread is None or not lifecycle._thread.is_alive()
