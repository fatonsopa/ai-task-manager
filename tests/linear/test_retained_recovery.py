"""T03 original-operation recovery over authored HTTP; no genuine provider effects."""

import json

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.application.linear_sync.inbound import InboundDrain
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import IssueStatus as S
from tests.linear.helpers import EXPECTED, STATES, LinearRig, ProviderFixture


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def updates(rig):
    return [r for r, _ in rig.provider.requests if "mutation UpdateIssue" in r["query"]]


def test_t03_lost_update_reply_uses_original_issue_without_resend(rig, monkeypatch):
    task = rig.transition(rig.task(), S.TODO, "enqueue", confirm=False)
    original = rig.client.update_issue

    def lost(*args):
        original(*args)
        raise LinearError("transport_failure", uncertain=True)

    monkeypatch.setattr(rig.client, "update_issue", lost)
    outcome = rig.outbound.run_once(rig.handle)
    operation_id = outcome["operation_id"]
    send = rig.journal.sends(rig.scope)[-1]
    assert outcome["state"] == "uncertain" and send["snapshot"] is None
    recovered = rig.outbound.confirm_pending(rig.handle, task.id)
    assert recovered.observed_status == S.TODO and len(updates(rig)) == 1
    assert rig.journal.sends(rig.scope)[-1]["id"] == send["id"]
    assert (
        next(o for o in rig.core.sync.list_operations(rig.handle) if o.id == operation_id).state
        == "confirmed"
    )


@pytest.mark.parametrize("race", ["human", "authority"])
def test_t03_changed_authority_after_readback_cannot_settle(rig, monkeypatch, race):
    task = rig.transition(rig.task(), S.TODO, "enqueue", confirm=False)
    original = rig.client.update_issue
    monkeypatch.setattr(
        rig.client,
        "update_issue",
        lambda *a: (original(*a), (_ for _ in ()).throw(LinearError("lost", uncertain=True)))[1],
    )
    outcome = rig.outbound.run_once(rig.handle)
    valid = [True]
    fired = []

    def hook(request, result):
        if "query ReadIssue" in request["query"] and not fired:
            fired.append(True)
            if race == "human":
                current = rig.current(task)
                proof = rig.proof(
                    "human_decision",
                    task.id,
                    {"previous_revision": current.decision_revision, "hold": True},
                )
                rig.core.decisions.record(rig.handle, task.id, current.version, proof, rig.key())
            else:
                valid[0] = False

    def current():
        if not valid[0]:
            raise AccessDenied("Authored finite authority was revoked")

    rig.provider.hook = hook
    with pytest.raises((LinearError, AccessDenied)):
        rig.reconcile.recover_outbound(rig.handle, outcome["operation_id"], check_authority=current)
    assert rig.current(task).observed_status == S.BACKLOG
    assert rig.current(task).sync_state != "confirmed" and len(updates(rig)) == 1


def test_t03_unsent_retry_rechecks_authority_before_rearming(rig):
    rig.task(confirmed=False)
    rig.client.binding = None
    rig.provider.fail_next = (503, b"unavailable")
    outcome = rig.outbound.run_once(rig.handle)
    with pytest.raises(AccessDenied):
        rig.reconcile.retry_unsent(
            rig.handle,
            outcome["operation_id"],
            check_authority=lambda: (_ for _ in ()).throw(AccessDenied("expired")),
        )
    operation = next(
        o for o in rig.core.sync.list_operations(rig.handle) if o.id == outcome["operation_id"]
    )
    assert operation.state == "failed" and not rig.provider.issues
    rig.reconcile.retry_unsent(rig.handle, operation.id, check_authority=lambda: None)
    assert rig.outbound.confirm_pending(rig.handle, operation.task_id).sync_state == "confirmed"
    assert len(rig.provider.issues) == 1


def test_t03_restart_preserves_applied_identity_and_foreign_pending_receipt(rig, monkeypatch):
    own, foreign = rig.task(), rig.task()
    for task in (own, foreign):
        rig.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        headers, body = rig.signed(rig.provider.event(task.issue_id))
        assert rig.receiver.receive("POST", headers, body).status == 200
    original = rig.core.sync.mark_applied
    monkeypatch.setattr(
        rig.core.sync,
        "mark_applied",
        lambda *a: (_ for _ in ()).throw(RuntimeError("after domain effect")),
    )
    with pytest.raises(RuntimeError):
        rig.inbound.run_once(rig.handle, task_ids=(own.id,))
    original_row = next(
        r
        for r in rig.journal.deliveries(rig.scope)
        if json.loads(r["payload"])["data"]["id"] == own.issue_id
    )
    assert rig.current(own).decision_revision == 1 and rig.current(foreign).decision_revision == 0
    monkeypatch.setattr(rig.core.sync, "mark_applied", original)
    rig.now += 31
    core = rig.new_core()
    journal = ProviderJournal.open_existing(rig.journal.path, clock=lambda: rig.now)
    restarted = InboundDrain(
        core, rig.client, journal, clock=lambda: rig.now, authorized_actors={EXPECTED.viewer_id}
    )
    assert restarted.run_once(rig.handle, task_ids=(own.id,))["state"] == "applied"
    current_row = next(r for r in journal.deliveries(rig.scope) if r["id"] == original_row["id"])
    assert (
        current_row["delivery_id"] == original_row["delivery_id"]
        and current_row["state"] == "applied"
    )
    assert core.get_task(rig.handle, own.id).decision_revision == 1
    assert core.get_task(rig.handle, foreign.id).decision_revision == 0
    assert (
        next(
            r
            for r in journal.deliveries(rig.scope)
            if json.loads(r["payload"])["data"]["id"] == foreign.issue_id
        )["state"]
        == "received"
    )


def test_t03_expired_execution_reports_proven_unsent_without_rearming(rig, monkeypatch, tmp_path):
    """Authored finite-authority port; real Core/outbox/journal, no provider send."""
    import time
    from dataclasses import asdict
    from types import SimpleNamespace

    from ai_task_manager.adapters.decisions.observations import intent
    from ai_task_manager.application.service.facade import ApplicationService
    from ai_task_manager.installation.workflows import provider_recovery

    task = rig.task(confirmed=False)
    operation = rig.core.sync.list_operations(rig.handle)[0]
    config_file = tmp_path / "authored-config.json"
    config = {"linear": {"authored": True}, "grant_ref": "authored-original-enrollment"}
    enrollment = {
        "scope": asdict(rig.scope),
        "config_path": str(config_file),
        "config_intent": intent(config),
        "provider": asdict(rig.client.binding.expected),
    }
    row = {"id": "authored-workflow", "task_id": task.id, "steps": {}}
    recovery = SimpleNamespace(
        config=config,
        config_file=config_file,
        journal=object(),
        data={"enrollment": enrollment, "enrollment_revocation": None},
        spec=SimpleNamespace(data={"deadline": time.time() - 1, "scope": asdict(rig.scope)}),
        check_service=lambda _: row,
    )
    monkeypatch.setattr(provider_recovery, "installed_authority", lambda current: current == config)
    monkeypatch.setattr(
        provider_recovery,
        "history",
        lambda journal, reference: enrollment if reference == config["grant_ref"] else None,
    )
    service = ApplicationService(
        rig.core,
        rig.handle,
        provider=rig.outbound,
        inbound=rig.inbound,
        seal=SimpleNamespace(check=lambda: None),
    )
    observed = provider_recovery.reconcile_original(recovery, service, row)
    assert observed["exit_code"] == 4 and observed["data"]["state"] == "checkpoint"
    assert observed["data"]["provider_recovery"][0]["state"] == "unsent"
    assert rig.core.sync.list_operations(rig.handle)[0] == operation
    assert not rig.provider.issues and not rig.journal.sends(rig.scope)


def test_t03_configured_consumers_recheck_revocation_during_provider_read(tmp_path):
    from tests.receiver.helpers import Fixture

    fixture = Fixture(tmp_path)
    try:
        rig = fixture.rig
        task = rig.transition(fixture.task, S.TODO, "enqueue", confirm=False)

        def lost(message, result):
            if "mutation UpdateIssue" in message["query"]:
                fixture.provider.fail_next = (503, b"lost readback")

        fixture.provider.hook = lost
        assert rig.outbound.run_once(rig.handle)["state"] == "uncertain"
        fixture.provider.hook = None
        service = fixture.load()
        mutations = sum("mutation" in message["query"] for message, _ in fixture.provider.requests)
        revoked = []

        def changed(message, result):
            if "query ReadIssue" in message["query"] and not revoked:
                revoked.append(True)
                service.decisions.observations.revoke()

        fixture.provider.hook = changed
        with pytest.raises(AccessDenied):
            service.provider.confirm_pending(service.handle, task.id)
        fixture.provider.hook = None
        assert revoked and rig.current(task).sync_state != "confirmed"
        assert rig.current(task).observed_status == S.BACKLOG
        with pytest.raises(AccessDenied):
            service.inbound.run_once(service.handle)
        with pytest.raises(AccessDenied):
            service.publisher(
                service.scope, service.core.get_task(service.handle, task.id), object()
            )
        assert (
            sum("mutation" in message["query"] for message, _ in fixture.provider.requests)
            == mutations
        )
        with rig.journal.transaction() as conn:
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='progress_metadata'"
            ).fetchone()
    finally:
        fixture.close()
