import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.application.linear_sync.outbound import OutboundDrain

from ai_task_manager.domains.work.models import IssueStatus as S
from tests.linear.helpers import STATES, LinearRig, ProviderFixture


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def test_l07_readback_failure_keeps_known_uuid_for_verified_recovery(rig):
    task = rig.task(confirmed=False)

    def hook(request, result):
        if "mutation CreateIssue" in request["query"]:
            rig.provider.fail_next = (503, b"unavailable")

    rig.provider.hook = hook
    outcome = rig.outbound.run_once(rig.handle)
    assert outcome["state"] == "uncertain"
    assert rig.current(task).issue_id is None and len(rig.provider.issues) == 1
    stored = rig.journal.sends(rig.scope)[0]
    assert stored["state"] == "uncertain" and json.loads(stored["snapshot"])["id"]
    rig.provider.hook = None
    assert rig.outbound.run_once(rig.handle) is None
    confirmed = rig.reconcile.recover_outbound(rig.handle, outcome["operation_id"])
    assert confirmed.observed_status == S.BACKLOG and len(rig.provider.issues) == 1


def test_l07_missing_create_identity_remains_uncertain_without_title_retry(rig, monkeypatch):
    task = rig.task(confirmed=False)
    original = rig.client.create_issue

    def lost_response(task):
        original(task)
        raise LinearError("transport_failure", uncertain=True)

    monkeypatch.setattr(rig.client, "create_issue", lost_response)
    outcome = rig.outbound.run_once(rig.handle)
    assert outcome["state"] == "uncertain" and len(rig.provider.issues) == 1
    with pytest.raises(LinearError, match="creation_identity_uncertain"):
        rig.reconcile.recover_outbound(rig.handle, outcome["operation_id"])
    assert rig.outbound.run_once(rig.handle) is None
    assert len(rig.provider.issues) == 1 and rig.current(task).issue_id is None


def test_l10_concurrent_drains_send_once(rig):
    rig.task(confirmed=False)
    drains = [
        OutboundDrain(rig.core, rig.client, rig.journal, adapter_id=f"adapter-{i}")
        for i in range(2)
    ]
    barrier = Barrier(2)

    def run(index):
        barrier.wait()
        return drains[index].run_once(rig.handle)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, range(2)))
    assert len([value for value in outcomes if value is not None]) == 1
    assert len(rig.provider.issues) == 1


def test_l22_crash_after_core_application_recovers_without_second_decision(rig, monkeypatch):
    task = rig.task()
    rig.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    rig.receiver.receive("POST", headers, body)
    original = rig.core.sync.mark_applied
    monkeypatch.setattr(
        rig.core.sync,
        "mark_applied",
        lambda *args: (_ for _ in ()).throw(RuntimeError("injected crash")),
    )
    with pytest.raises(RuntimeError):
        rig.inbound.run_once(rig.handle)
    current = rig.current(task)
    assert current.observed_status == S.TODO and current.decision_revision == 1
    monkeypatch.setattr(rig.core.sync, "mark_applied", original)
    rig.now += 31
    result = rig.inbound.run_once(rig.handle)
    assert result["state"] == "applied"
    assert rig.current(task).decision_revision == 1


def test_l20_webhook_before_outbound_readback_is_pending_until_confirmation(rig):
    task = rig.task()
    task = rig.transition(task, S.TODO, "enqueue", confirm=False)
    during = []

    def hook(request, result):
        if "mutation UpdateIssue" in request["query"]:
            headers, body = rig.signed(rig.provider.event(task.issue_id))
            assert rig.receiver.receive("POST", headers, body).status == 200
            during.append(rig.inbound.run_once(rig.handle))

    rig.provider.hook = hook
    assert rig.outbound.run_once(rig.handle)["state"] == "confirmed"
    rig.provider.hook = None
    assert during == [{"state": "pending", "reason": "awaiting_outbound_readback"}]
    assert rig.inbound.run_once(rig.handle)["classification"] == "outbound_echo"
    assert rig.current(task).decision_revision == 0


def test_l23_recovery_inspects_registered_only_without_backlog_import(rig):
    task = rig.task()
    issue = rig.provider.issues[task.issue_id]
    from tests.linear.helpers import uid

    rig.provider.issues[uid(800)] = {**issue, "id": uid(800), "title": "Foreign unrelated issue"}
    before = len(rig.provider.requests)
    result = rig.reconcile.inspect_registered(rig.handle)
    assert result == [{"task_id": task.id, "state": "matching"}]
    assert len(rig.provider.requests) == before + 1
    assert len(rig.core.list_tasks(rig.handle)) == 1


def test_l25_provider_journal_restart_schema_and_secrets(rig):
    task = rig.task()
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    rig.receiver.receive("POST", headers, body)
    reopened = ProviderJournal(rig.journal.path)
    assert len(reopened.deliveries(rig.scope)) == 1
    assert len(reopened.sends(rig.scope)) == 1
    raw = Path(rig.journal.path).read_bytes()
    assert b"fixture-api-secret" not in raw and b"fixture-webhook-signing-secret" not in raw
    assert Path(rig.journal.path).stat().st_mode & 0o777 == 0o600
    with reopened.transaction() as conn:
        conn.execute("UPDATE metadata SET version=999")
    with pytest.raises(LinearError, match="unsupported_provider_schema"):
        ProviderJournal(rig.journal.path)


@pytest.mark.parametrize("phase", ["before_send", "after_effect"])
def test_l09_racing_local_human_hold_blocks_stale_send_or_confirmation(rig, phase):
    task = rig.task()
    task = rig.transition(task, S.TODO, "enqueue", confirm=False)
    fired = []

    def hook(request, result):
        match = "query ReadIssue" if phase == "before_send" else "mutation UpdateIssue"
        if match in request["query"] and not fired:
            fired.append(True)
            current = rig.current(task)
            ref = rig.proof(
                "human_decision",
                task.id,
                {"previous_revision": current.decision_revision, "hold": True},
            )
            rig.core.decisions.record(rig.handle, task.id, current.version, ref, rig.key())

    rig.provider.hook = hook
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == ("failed" if phase == "before_send" else "uncertain")
    assert rig.current(task).human_hold and rig.current(task).observed_status == S.BACKLOG
    updates = [
        request
        for request, _ in rig.provider.requests
        if "mutation UpdateIssue" in request["query"]
    ]
    assert len(updates) == (0 if phase == "before_send" else 1)
    assert rig.outbound.run_once(rig.handle) is None


def test_l22_conflicted_hold_settles_only_after_trusted_stop_evidence_arrives(rig):
    task, attempt = rig.running()
    rig.provider.edit(task.issue_id, state={"id": STATES["On Hold"], "name": "On Hold"})
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    receipt = rig.journal.deliveries(rig.scope)[0]
    stop = rig.proof("quiescence", task.id, {"attempt_id": attempt.id, "quiescent": True})
    rig.inbound.evidence_for = lambda task, event: {"quiescence": stop}
    rig.inbound.retry_conflict(rig.handle, receipt["id"])
    result = rig.inbound.run_once(rig.handle)
    assert result["state"] == "applied" and rig.current(task).observed_status == S.ON_HOLD
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "paused"


def test_l23_missed_hold_event_recovery_fences_work_without_inventing_actor(rig):
    task, attempt = rig.running()
    rig.provider.edit(task.issue_id, state={"id": STATES["On Hold"], "name": "On Hold"})
    assert rig.reconcile.inspect_registered(rig.handle) == [
        {"task_id": task.id, "state": "conflict"}
    ]
    assert rig.current(task).human_hold and rig.current(task).decision_revision == 0
    assert rig.current(task).observed_status == S.IN_PROGRESS


def test_l02_failure_before_mutation_can_retry_only_with_durable_unsent_proof(rig):
    task = rig.task(confirmed=False)
    rig.client.binding = None
    rig.provider.fail_next = (503, b"unavailable")
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "failed" and not rig.provider.issues
    rig.reconcile.retry_unsent(rig.handle, result["operation_id"])
    assert rig.outbound.confirm_pending(rig.handle, task.id).observed_status == S.BACKLOG
