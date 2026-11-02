"""Owned SQLite/provider/protocol controls, not live account or TST evidence."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.accounting import AccountingPlan
from ai_task_manager.adapters.hosts.bridge.admission import NativeAdmissionVerifier
from ai_task_manager.adapters.hosts.bridge.evaluation_decisions import EvaluationDecisions
from ai_task_manager.adapters.hosts.bridge.evaluation_enrollment import (
    EvaluationEnrollment,
    EvaluationGrant,
)
from ai_task_manager.adapters.linear.models import VerifiedBinding
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import Approval, Evidence, TaskProse
from tests.domains.helpers import ProofFixture
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.bootstrap_helpers import grant_graph


def enroll(g):
    permit = g.item.accounting.claim(g.item.policy, g.item.binding, "enrollment", maximum=20)
    destination = g.item.root / "session.json"
    handle = EvaluationEnrollment(g.grant, g.client, g.provider).establish(permit, destination)
    assert SessionFile(destination).load(g.core) == handle
    return handle


def task(g, handle):
    """Authored writer/provider observations drive real Core intake and confirmation."""
    proof = ProofFixture()
    proof.approvals["fixture:writing"] = Approval(
        g.body["task_prose_digest"], g.core.context.approval_policy, "exact-v1"
    )
    g.core.context.approvals = proof
    prose = TaskProse(
        "Verify an owned data report",
        "Summarize the owned fixture dataset and verify its output.",
        False,
        ("complete",),
    )
    item = g.core.intake.submit(handle, prose, "fixture:writing", "authored-intake")
    operation = g.core.sync.list_operations(handle)[-1]
    proof.evidence["fixture:provider"] = Evidence(
        "provider_status",
        g.grant.scope,
        item.id,
        {
            "operation_id": operation.id,
            "status": "Backlog",
            "prose_digest": item.prose_digest,
            "title": item.title,
            "body": item.body,
            "representation": item.representation,
            "issue_id": "authored-issue",
        },
    )
    g.core.context.evidence = proof
    item = g.core.intake.confirm_status(
        handle,
        item.id,
        item.version,
        item.decision_revision,
        "fixture:provider",
        "authored-provider-confirm",
    )
    g.grant.bind_task(handle, item.id)
    return item


def test_actual_enrollment_uses_verified_provider_and_enrolled_fresh_core(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    handle = enroll(g)
    _, scope = g.core.session_binding(handle)
    assert scope == g.grant.scope
    assert g.core.list_tasks(handle) == []
    assert not g.item.native.startup.admitted
    assert g.item.native.gateway.delegate is None
    assert g.grant.verify_enrollment(g.ref).source_ref == g.ref
    with pytest.raises(AccessDenied):
        enroll(g)


@pytest.mark.parametrize(
    "change",
    [
        "purpose",
        "project",
        "actions",
        "process",
        "configuration",
        "store",
        "model",
        "effort",
        "expired",
    ],
)
def test_pinned_grant_refuses_scope_or_source_drift(bootstrap_graph, change):
    g = grant_graph(bootstrap_graph())
    data = json.loads(json.dumps(g.body))
    if change == "purpose":
        data["purpose"] = "normal-session"
    elif change == "project":
        data["scope"]["project"] = "foreign-project"
    elif change == "actions":
        data["actions"] = ["request_review", "release"]
    elif change == "process":
        data["bootstrap_process"]["pid"] += 1
    elif change == "configuration":
        data["bootstrap_configuration_digest"] = "foreign"
    elif change == "store":
        data["claim_store"]["store_id"] = "foreign"
    elif change == "model":
        data["model"] = "foreign"
    elif change == "effort":
        data["effort"] = "low"
    else:
        data["expires_at"] = 1
    ref = g.item.accounting.rig.pin("evaluation_grant", data)
    from tools.build_support.authorization import PinnedRecords

    with pytest.raises(AccessDenied):
        EvaluationGrant(
            PinnedRecords(g.item.accounting.rig.bindings),
            ref,
            "policy-fixture",
            g.item.observations,
            g.core,
            g.claim,
            g.item.journal,
            g.item.expected,
        )


def test_changed_record_and_revocation_cannot_issue_current_authority(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    prepared = g.grant.prepare_current()
    g.grant.revoke()
    with pytest.raises(AccessDenied):
        prepared.check()
    with pytest.raises(AccessDenied):
        g.grant.verify_enrollment(g.ref)
    g = grant_graph(bootstrap_graph())
    prepared = g.grant.prepare_current()
    path = g.grant.source.path
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(AccessDenied):
        prepared.check()
    with pytest.raises(Exception):
        g.grant.require_active()


def test_wrong_provider_does_not_create_core_session(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    g.client = SimpleNamespace(
        verify_binding=lambda: VerifiedBinding(replace(g.item.expected, viewer_id="foreign"), {})
    )
    with pytest.raises(AccessDenied):
        enroll(g)
    with g.core.context.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_grant_cannot_issue_human_or_notification_authority(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    with pytest.raises(EvidenceRequired):
        g.grant.verify_instruction(g.ref)
    with pytest.raises(EvidenceRequired):
        g.grant.verify_host_observation(g.ref)
    with pytest.raises(AccessDenied):
        g.grant.verify_enrollment("unregistered")


def test_one_confirmed_non_code_task_is_bound_without_renewal(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    handle = enroll(g)
    item = task(g, handle)
    g.grant.require_task(item)
    with pytest.raises(AccessDenied):
        g.grant.bind_task(handle, item.id)
    with pytest.raises(AccessDenied):
        g.grant.require_task(replace(item, id="foreign"))
    with pytest.raises(AccessDenied):
        g.grant.require_task(replace(item, code_bearing=True))


def test_native_admission_requires_actual_observation_exact_task_policy_and_root(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    handle = enroll(g)
    item = task(g, handle)
    verifier = NativeAdmissionVerifier(g.item.observations, g.body["bootstrap_ref"], g.grant)
    plan = AccountingPlan(
        "batch",
        "evaluated",
        item.id,
        "claude",
        g.item.policy.model,
        "high",
        "authored-billing-control",
        g.body["expires_at"],
        15,
        5,
        5,
        10,
    )
    verifier.check_admission(plan)
    with pytest.raises(AccessDenied):
        verifier.check_admission(replace(plan, model="foreign"))
    with pytest.raises(AccessDenied):
        verifier.check_admission(replace(plan, task_id="foreign"))
    with pytest.raises(AccessDenied):
        verifier.require_native_conformance(g.item.policy, g.item.binding, g.item.journal)
    with pytest.raises(AccessDenied):
        verifier.verify_policy(g.item.policy, g.item.binding, plan, object())


def test_evaluation_decision_consumption_checks_current_grant_without_full_reads(
    bootstrap_graph, monkeypatch
):
    g = grant_graph(bootstrap_graph())
    handle = enroll(g)
    item = task(g, handle)
    decisions = EvaluationDecisions(g.grant, g.authority)
    ref = decisions.decision(
        g.grant.scope, item, "Todo", "enqueue", grant_ref=g.ref, reason="Authored evaluation case"
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Full source/history read under Core")

    monkeypatch.setattr(g.grant, "require_active", forbidden)
    monkeypatch.setattr(g.item.observations, "verify", forbidden)
    monkeypatch.setattr(g.grant.source, "check", forbidden)
    with g.core.context.db.transaction():
        assert decisions.verify_evidence(ref).payload["actor"] == "agent"
    g.grant.revoke()
    with g.core.context.db.transaction(), pytest.raises(AccessDenied):
        decisions.verify_evidence(ref)


def test_mutated_scope_or_cached_grant_cannot_bypass_prepared_consumption(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    prepared = g.grant.prepare_current()
    g.grant.scope = replace(g.grant.scope, project="foreign")
    with pytest.raises(AccessDenied):
        prepared.check()
    g = grant_graph(bootstrap_graph())
    prepared = g.grant.prepare_current()
    g.grant.data["expires_at"] += 100
    with pytest.raises(AccessDenied):
        prepared.check()
