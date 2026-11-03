"""Real enrolled grants; authored parent/prose values isolate issuance boundaries."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.decisions.authority import DecisionAuthority
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.sessions import setup
from tests.session_observations.helpers import OwnedProvider


@pytest.fixture
def enrolled(tmp_path):
    provider = OwnedProvider(tmp_path / "owned")
    try:
        setup(
            provider.config,
            logical_session="lead-session",
            actions=("delivered_followup",),
            deadline=provider.deadline,
            operation_key="followup-setup",
            components=provider.components(),
        )
        service = load_service(provider.config, linear_components=provider.components())
        yield provider, service
    finally:
        provider.close()


def issue(service, **changes):
    values = dict(
        scope=service.scope,
        parent=SimpleNamespace(id="authored-parent"),
        prose_digest="a" * 64,
        grant_ref=service.grant_ref,
    )
    values.update(changes)
    return service.decisions.delivered_followup(**values)


def test_followup_proof_routes_exact_scope_parent_prose_and_short_expiry(enrolled):
    provider, service = enrolled
    reference = issue(service)
    evidence = service.core.context.evidence.verify_evidence(reference)
    assert (evidence.kind, evidence.scope, evidence.subject) == (
        "delivered_followup",
        service.scope,
        "authored-parent",
    )
    assert evidence.payload == {"prose_digest": "a" * 64, "target": "Backlog"}
    assert evidence.expires_at == provider.deadline
    assert (
        service.decisions.journal._get(reference, "decision-proof:")["source_ref"]
        == service.grant_ref
    )
    service.decisions.observations.revoke()
    with pytest.raises(AccessDenied, match="revoked"):
        service.core.context.evidence.verify_evidence(reference)


@pytest.mark.parametrize("fault", ["foreign_scope", "foreign_ref", "expired", "revoked"])
def test_invalid_enrollment_cannot_issue_followup(enrolled, fault):
    provider, service = enrolled
    changes = {}
    if fault == "foreign_scope":
        changes["scope"] = replace(service.scope, logical_session="foreign")
    elif fault == "foreign_ref":
        changes["grant_ref"] = "session-enrollment:foreign"
    elif fault == "expired":
        service.decisions.journal.clock = lambda: provider.deadline
    else:
        service.decisions.observations.revoke()
    with pytest.raises(AccessDenied):
        issue(service, **changes)


def test_expiry_is_checked_again_by_the_production_router(enrolled):
    provider, service = enrolled
    reference = issue(service)
    service.decisions.journal.clock = lambda: provider.deadline
    with pytest.raises(AccessDenied, match="stale"):
        service.core.context.evidence.verify_evidence(reference)


def test_short_proof_lifetime_does_not_renew_long_enrollment(enrolled):
    _, service = enrolled
    journal = service.decisions.journal
    original_clock = journal.clock
    # Move only this control's clock backwards; the signed grant remains unchanged.
    now = original_clock() - 600
    journal.clock = lambda: now
    reference = issue(service)
    assert journal.verify_evidence(reference).expires_at == now + 300
    journal.clock = lambda: now + 300
    with pytest.raises(AccessDenied, match="stale"):
        journal.verify_evidence(reference)


def test_missing_observer_never_accepts_caller_claims(enrolled):
    _, service = enrolled
    authority = DecisionAuthority(service.decisions.journal)
    with pytest.raises(EvidenceRequired):
        authority.delivered_followup(
            service.scope, SimpleNamespace(id="parent"), "a" * 64, grant_ref='{"authorized":true}'
        )
