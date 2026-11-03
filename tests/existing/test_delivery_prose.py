"""New prose authority consults observed delivery; replay preserves historical facts."""

import time
from uuid import uuid4

import pytest

from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.application.linear_sync.inbound import InboundDrain
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition, SchemaError
from ai_task_manager.domains.work.models import Evidence, TaskProse
from ai_task_manager.storage import issue_claims
from tests.domains.helpers import ProofFixture
from tests.linear.helpers import EXPECTED, STATES

from .helpers import span


def observe_delivery_reset(rig, provider, task):
    """Complete authored HTTP history, not a caller assertion of delivery."""
    start, reset = "2026-11-01T12:00:00Z", "2026-11-01T12:01:00Z"
    provider.histories[task.issue_id][-1]["endedAt"] = start
    provider.histories[task.issue_id].extend(
        [span("Done", start, reset, number=20), span("Todo", reset, number=21)]
    )
    provider.issues[task.issue_id]["state"] = {"id": STATES["Todo"], "name": "Todo"}
    provider.issues[task.issue_id]["updatedAt"] = "2026-11-01T12:02:00Z"
    before = rig.effects()
    inspected = rig.intake.inspect(task.issue_id)
    assert inspected["history"]["assessment"] == "blocked"
    assert rig.effects() == before
    assert rig.core.get_task(rig.handle, task.id) == task and not task.delivered_once


def human_decision(rig, task, target=None):
    return rig.authority._evidence(
        "decision",
        rig.scope,
        task.id,
        {
            "target": target or task.observed_status,
            "revision": task.decision_revision,
            "actor": "human",
            "reason": "Synthetic authorized scope-control fixture",
        },
        "synthetic-human-observer-only",
    )


def record_external(rig, provider, task):
    snapshot = rig.client.read_issue(task.issue_id)
    proof = rig.journal.external_receipt(
        rig.scope, task, snapshot, provider.event(task.issue_id), {EXPECTED.viewer_id}
    )
    return rig.core.external.observe(
        rig.handle, task.id, task.version, task.decision_revision, proof, str(uuid4())
    )


def test_ea82_known_delivery_blocks_local_revision_without_rewriting_task(rig, provider):
    task = rig.admit(provider.seed("Todo"))
    observe_delivery_reset(rig, provider, task)
    prose = TaskProse(
        "Verify newly reported behavior", "Retain the distinct investigation evidence."
    )
    before = rig.effects()
    with pytest.raises(InvalidTransition, match="quiescent undelivered"):
        rig.core.intake.revise(
            rig.handle,
            task.id,
            prose,
            rig.approvals.add(rig.scope, prose),
            task.version,
            task.decision_revision,
            human_decision(rig, task),
            str(uuid4()),
        )
    assert rig.effects() == before
    rig.assert_no_provider_mutations()


def test_ea82_delivery_allows_observation_but_not_staged_new_scope(rig, provider):
    task = rig.admit(provider.seed("Todo"))
    observe_delivery_reset(rig, provider, task)
    prose = TaskProse("Verify external follow-up", "Keep the reported change as observed data.")
    provider.issues[task.issue_id].update(
        title=prose.title, description=prose.body, updatedAt="2026-11-01T12:03:00Z"
    )
    observed = record_external(rig, provider, task)
    assert observed["state"] == "pending"
    current = rig.core.get_task(rig.handle, task.id)
    assert current.body == task.body and not current.delivered_once
    assert any(
        event.kind == "external_prose_change_staged"
        for event in rig.core.notifications.list_events(rig.handle)
    )
    inbound = InboundDrain(
        rig.core, rig.client, rig.journal, clock=time.time, authorized_actors={EXPECTED.viewer_id}
    )
    fresh = inbound.fresh_readback_ref(rig.handle, task.id, observed["observation_id"])
    before = rig.effects()
    with pytest.raises(EvidenceRequired, match="undelivered scope"):
        rig.core.external.approve_prose_revision(
            rig.handle,
            task.id,
            observed["observation_id"],
            prose,
            rig.approvals.add(rig.scope, prose),
            current.version,
            current.decision_revision,
            fresh,
            str(uuid4()),
        )
    assert rig.effects() == before
    rig.assert_no_provider_mutations()


def test_ea82_canonical_positive_parent_requires_separate_exact_followup_proof(rig, provider):
    task = rig.admit(provider.seed("Todo"))
    observe_delivery_reset(rig, provider, task)
    prose = TaskProse("Verify a separate follow-up", "Investigate the new report in its own issue.")
    approved = rig.approvals.add(rig.scope, prose)
    before = rig.effects()
    with pytest.raises(EvidenceRequired):
        rig.core.intake.submit(rig.handle, prose, approved, str(uuid4()), parent_task_id=task.id)
    assert rig.effects() == before
    # Retain this original synthetic consumer control. Production now owns the
    # kind, so only this copied test route relinquishes it to the authored fixture.
    # Installed producer/router coverage lives in test_followup_submission.py.
    facts = ProofFixture()
    routes = [
        EvidenceRoute(route.prefix, route.kinds - {"delivered_followup"}, route.verifier)
        for route in rig.router.routes
    ]
    rig.core.context.evidence = EvidenceRouter(
        [*routes, EvidenceRoute("synthetic-followup:", {"delivered_followup"}, facts)]
    )
    ref = "synthetic-followup:" + str(uuid4())
    digest = prose_digest(prose, rig.renderer.render(prose))
    facts.evidence[ref] = Evidence(
        "delivered_followup", rig.scope, task.id, {"prose_digest": "wrong", "target": "Backlog"}
    )
    with pytest.raises(EvidenceRequired, match="new Backlog"):
        rig.core.intake.submit(
            rig.handle, prose, approved, str(uuid4()), parent_task_id=task.id, followup_ref=ref
        )
    assert rig.effects() == before
    facts.evidence[ref] = Evidence(
        "delivered_followup", rig.scope, task.id, {"prose_digest": digest, "target": "Backlog"}
    )
    new = rig.core.intake.submit(
        rig.handle, prose, approved, str(uuid4()), parent_task_id=task.id, followup_ref=ref
    )
    assert new.parent_task_id == task.id and new.id != task.id
    assert new.desired_status == "Backlog" and new.observed_status is None
    assert not new.queued_once and not rig.effects()["attempts"]
    assert len(rig.effects()["tasks"]) == 2 and len(rig.effects()["outbox"]) == 1
    rig.assert_no_provider_mutations()


def test_ea82_committed_scope_replay_precedes_later_delivery_fence(rig, provider):
    task = rig.admit(provider.seed())
    prose = TaskProse("Verify the approved revision", "Retain exact evidence for this scope.")
    approved, decision, key = (
        rig.approvals.add(rig.scope, prose),
        human_decision(rig, task),
        str(uuid4()),
    )
    args = (rig.handle, task.id, prose, approved, task.version, task.decision_revision, decision)
    revised = rig.core.intake.revise(*args, key)
    current = OutboundDrain(rig.core, rig.client, rig.journal).confirm_pending(rig.handle, task.id)
    observe_delivery_reset(rig, provider, current)
    before = rig.effects()
    assert rig.core.intake.revise(*args, key) == revised
    assert rig.effects() == before
    with pytest.raises(InvalidTransition):
        rig.core.intake.revise(
            rig.handle,
            current.id,
            prose,
            approved,
            current.version,
            current.decision_revision,
            human_decision(rig, current),
            str(uuid4()),
        )
    assert rig.effects() == before


@pytest.mark.parametrize("value", [-1, 2, 0.5, "invalid"])
def test_ea82_malformed_canonical_fact_cannot_be_interpreted_as_undelivered(rig, provider, value):
    task = rig.admit(provider.seed())
    with rig.db.transaction() as conn:
        conn.execute(
            "INSERT INTO issue_lifecycle VALUES(?,?,?,?)",
            (issue_claims.identity(rig.scope, task.issue_id), value, 0, "fixture"),
        )
    prose = TaskProse("Verify malformed facts", "Refuse the malformed authority record.")
    before = rig.effects()
    with pytest.raises(SchemaError):
        rig.core.intake.revise(
            rig.handle,
            task.id,
            prose,
            rig.approvals.add(rig.scope, prose),
            task.version,
            task.decision_revision,
            human_decision(rig, task),
            str(uuid4()),
        )
    assert rig.effects() == before
