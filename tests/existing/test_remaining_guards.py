import time
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import Credentials
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    CapacityExceeded,
    EvidenceRequired,
    StaleVersion,
)
from ai_task_manager.storage import work

from .helpers import STATES, span


@pytest.mark.parametrize("mismatch", ["issue", "digest", "scope", "expiry", "store"])
def test_ea26_valid_signed_selection_cannot_authorize_different_context(rig, provider, mismatch):
    issue = provider.seed()
    ref, prose, approval, selected = rig.packet(issue)
    source = rig.authority.verify_evidence(selected)
    payload = dict(source.payload)
    scope, subject, expiry = source.scope, source.subject, time.time() + 300
    if mismatch == "issue":
        subject = str(uuid4())
    elif mismatch == "digest":
        payload["discovery_digest"] = "f" * 64
    elif mismatch == "scope":
        scope = replace(scope, logical_session="other")
    elif mismatch == "store":
        payload["claim_store"] = {"installation_id": str(uuid4()), "store_id": str(uuid4())}
    else:
        expiry = time.time() - 1
    selected = rig.authority._evidence(
        "existing_issue_selection", scope, subject, payload, "synthetic-negative-control", expiry
    )
    before = rig.effects()
    expected = AccessDenied if mismatch == "expiry" else EvidenceRequired
    with pytest.raises(expected):
        rig.core.existing.admit(rig.handle, ref, ref, prose, approval, selected, str(uuid4()))
    assert rig.effects() == before


def test_ea10_credentials_cannot_silently_switch_after_verified_binding(rig, provider):
    issue = provider.seed()
    rig.client.credentials = lambda: Credentials("different-synthetic-account")
    before = rig.effects()
    with pytest.raises(LinearError, match="credential_binding_changed"):
        rig.reader.inspect(issue)
    assert rig.effects() == before


def test_ea10_transport_timeout_has_no_creation_fallback(rig, provider, monkeypatch):
    issue = provider.seed()
    before = rig.effects()

    def timeout(*args, **kwargs):
        raise LinearError("transport_timeout", uncertain=True)

    monkeypatch.setattr(rig.client.transport, "send", timeout)
    with pytest.raises(LinearError) as caught:
        rig.reader.inspect(issue)
    assert not caught.value.uncertain  # Query timeout has no unconfirmed remote mutation.
    assert rig.effects() == before
    rig.assert_no_provider_mutations()


def test_ea03_current_authenticated_workspace_must_match(rig, provider):
    from ai_task_manager.adapters.linear.existing import ExistingReader

    with pytest.raises(AccessDenied):
        ExistingReader(
            rig.client, rig.journal, replace(rig.scope, workspace=str(uuid4())), rig.claim.context
        )


def queue_todo(rig, task):
    ref = rig.intake.guard(task)
    return rig.core.transitions.transition(
        rig.handle,
        task.id,
        "Todo",
        "enqueue",
        task.version,
        task.decision_revision,
        rig.decision(task),
        {"existing_source": ref},
        str(uuid4()),
    )


def test_ea30_active_own_binding_reuses_attempt_and_rejects_duplicate_reservation(rig, provider):
    issue = provider.seed("Todo")
    task = queue_todo(rig, rig.admit(issue))
    rig.intake.guard(task, startup=True)
    attempt = rig.core.execution.reserve(
        rig.handle,
        task.id,
        "fixture",
        task.version,
        task.decision_revision,
        rig.decision(task, "In Progress"),
        str(uuid4()),
    )
    current = rig.core.get_task(rig.handle, task.id)
    assert rig.admit(issue) == current
    assert rig.core.get_attempt(rig.handle, attempt.id) == attempt
    rig.intake.guard(current, startup=True)
    with pytest.raises(CapacityExceeded):
        rig.core.execution.reserve(
            rig.handle,
            current.id,
            "fixture",
            current.version,
            current.decision_revision,
            rig.decision(current, "In Progress"),
            str(uuid4()),
        )
    assert len(rig.effects()["attempts"]) == 1


@pytest.mark.parametrize("status", ["On Hold", "In Review"])
def test_ea32_own_guarded_binding_does_not_release_human_hold(rig, provider, status):
    issue = provider.seed("Todo")
    task = queue_todo(rig, rig.admit(issue))
    # Explicit trusted fixture represents an already retained guarded own lifecycle;
    # import does not manufacture this state or its previous queue history.
    provider.issues[issue]["state"] = {"id": STATES[status], "name": status}
    provider.histories[issue][-1]["endedAt"] = "2026-11-01T00:01:30Z"
    provider.histories[issue].append(span(status, "2026-11-01T00:01:30Z", number=2))
    with rig.db.transaction() as conn:
        task = work.save(
            conn,
            replace(task, observed_status=status, desired_status=status, human_hold=True),
            task.version,
        )
    reused = rig.admit(issue)
    assert reused.human_hold and reused.queued_once and reused.observed_status == status
    source = rig.intake.guard(reused)
    before = rig.effects()
    with pytest.raises((EvidenceRequired, StaleVersion)):
        rig.core.transitions.transition(
            rig.handle,
            task.id,
            "Todo",
            "enqueue",
            task.version,
            task.decision_revision,
            rig.decision(task),
            {"existing_source": source},
            str(uuid4()),
        )
    assert rig.effects() == before


def test_ea36_backlog_queue_uses_original_outbox_and_exact_todo_readback(rig, provider):
    from ai_task_manager.application.linear_sync.outbound import OutboundDrain

    issue = provider.seed()
    task = rig.admit(issue)
    ref = rig.intake.guard(task)
    requested = rig.core.transitions.transition(
        rig.handle,
        task.id,
        "Todo",
        "enqueue",
        task.version,
        task.decision_revision,
        rig.decision(task),
        {"existing_source": ref},
        str(uuid4()),
    )
    assert requested.sync_state == "pending" and not requested.queued_once
    confirmed = OutboundDrain(rig.core, rig.client, rig.journal).confirm_pending(
        rig.handle, task.id
    )
    assert confirmed.queued_once and confirmed.observed_status == "Todo"
    assert len(rig.effects()["outbox"]) == 1
    mutations = [request for request, _ in provider.requests if "mutation " in request["query"]]
    assert len(mutations) == 1 and "mutation UpdateIssue" in mutations[0]["query"]
    assert len(provider.issues) == 1
    rig.intake.guard(confirmed, startup=True)


def test_ea46_new_human_hold_between_source_read_and_queue_commit_wins(rig, provider):
    issue = provider.seed("Todo")
    task = rig.admit(issue)
    source = rig.intake.guard(task)
    human = rig.authority._evidence(
        "human_decision",
        rig.scope,
        task.id,
        {"previous_revision": task.decision_revision, "hold": True},
        "synthetic-observed-human",
    )
    held = rig.core.decisions.record(rig.handle, task.id, task.version, human, str(uuid4()))
    before = rig.effects()
    with pytest.raises(StaleVersion):
        rig.core.transitions.transition(
            rig.handle,
            task.id,
            "Todo",
            "enqueue",
            task.version,
            task.decision_revision,
            rig.decision(task),
            {"existing_source": source},
            str(uuid4()),
        )
    assert rig.core.get_task(rig.handle, task.id) == held and rig.effects() == before


def test_ea45_current_revision_change_after_inspection_prevents_binding(rig, provider):
    issue = provider.seed()
    packet = rig.packet(issue)
    provider.issues[issue]["updatedAt"] = "2026-11-01T00:03:00Z"
    before = rig.effects()
    with pytest.raises(EvidenceRequired, match="Issue changed"):
        rig.intake.admit(*packet, str(uuid4()))
    assert rig.effects() == before


def test_ea52_changed_discovery_receipt_digest_is_not_trusted(rig, provider):
    issue = provider.seed()
    ref, prose, approved, selected = rig.packet(issue)
    with rig.journal.transaction() as conn:
        import json

        data = json.loads(
            conn.execute("SELECT payload FROM proofs WHERE ref=?", (ref,)).fetchone()[0]
        )
        data["discovery_digest"] = "f" * 64
        conn.execute("UPDATE proofs SET payload=? WHERE ref=?", (json.dumps(data), ref))
    with pytest.raises(EvidenceRequired):
        rig.intake.admit(ref, prose, approved, selected, str(uuid4()))
    assert not rig.core.list_tasks(rig.handle)
