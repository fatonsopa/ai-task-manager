"""Later human Backlog decisions, with an authored HTTP provider and real policy."""

import pytest

from ai_task_manager.domains.work.models import IssueStatus as S
from tests.linear.helpers import STATES, LinearRig, ProviderFixture


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()
        assert not provider.thread.is_alive()


def test_hb01_pending_response_human_backlog_retains_hold_and_old_intent_fence(rig):
    task = rig.task()
    task = rig.transition(task, S.TODO, "enqueue", confirm=False)
    captured = []

    def hook(request, _):
        if "mutation UpdateIssue" in request["query"]:
            rig.provider.edit(task.issue_id, state={"id": STATES["Backlog"], "name": "Backlog"})
            headers, body = rig.signed(rig.provider.event(task.issue_id))
            assert rig.receiver.receive("POST", headers, body).status == 200
            assert rig.inbound.run_once(rig.handle)["state"] == "applied"
            captured.append(rig.current(task))

    rig.provider.hook = hook
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain" and result["error"] == "readback_race"
    current = rig.current(task)
    operation = rig.core.sync.list_operations(rig.handle)[-1]
    assert current == captured[0]
    assert current.observed_status == current.desired_status == S.BACKLOG
    assert current.decision_revision == 1 and operation.fenced_by
    assert current.human_hold
    assert rig.outbound.run_once(rig.handle) is None
    assert (
        sum("mutation UpdateIssue" in request["query"] for request, _ in rig.provider.requests) == 1
    )


def test_hb07_initial_backlog_echo_is_not_a_hold_but_new_human_backlog_is(rig):
    task = rig.task()
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    assert rig.receiver.receive("POST", headers, body).status == 200
    assert rig.inbound.run_once(rig.handle)["classification"] == "outbound_echo"
    assert not rig.current(task).human_hold and rig.current(task).decision_revision == 0
    rig.provider.edit(task.issue_id, title=task.title)
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    assert rig.receiver.receive("POST", headers, body).status == 200
    assert rig.inbound.run_once(rig.handle)["state"] == "applied"
    held = rig.current(task)
    assert held.human_hold and held.decision_revision == 1
    assert rig.receiver.receive("POST", headers, body).status == 200
    assert rig.inbound.run_once(rig.handle) is None
    assert rig.current(task) == held
    assert len(rig.journal.sends(rig.scope)) == 1


def human_service(rig, tmp_path, *, provider=True):
    from ai_task_manager.adapters.decisions.authority import DecisionAuthority
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.application.service.facade import ApplicationService
    from tests.application.helpers import EvidenceRoutes, Observations

    journal = AuthorityJournal(tmp_path / "authority.sqlite", tmp_path / "authority.key")
    observations = Observations()
    authority = DecisionAuthority(journal, observations)
    rig.core.context.evidence = EvidenceRoutes(journal, rig.core.context.evidence)
    service = ApplicationService(
        rig.core, rig.handle, decisions=authority, provider=rig.outbound if provider else None
    )
    return service, observations


def human_action(service, observations, task, target, key):
    from ai_task_manager.adapters.decisions.authority import HumanInstruction

    observations.instructions[key] = HumanInstruction(
        service.scope,
        task.id,
        target,
        task.decision_revision,
        "Authored human decision",
        "authored-observation-" + key,
        task.version,
        task.prose_digest,
    )
    return service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": {"Backlog": "dequeue", "Todo": "enqueue", "On Hold": "hold"}[target],
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": key,
            "human_ref": key,
        },
    )


@pytest.mark.parametrize("target", ["Backlog", "On Hold"])
@pytest.mark.parametrize("loss", [None, "response", "readback"])
def test_hb09_real_http_publishes_only_current_hold_and_recovers_without_release(
    rig, tmp_path, monkeypatch, loss, target
):
    from ai_task_manager.adapters.linear.errors import LinearError
    from ai_task_manager.application.linear_sync.outbound import OutboundDrain
    from ai_task_manager.application.linear_sync.reconcile import Reconciliation

    if target == "Backlog":
        task = rig.queued()
    else:
        task, attempt = rig.running()
        task, _ = rig.settle(task, attempt, state="paused")
    service, observations = human_service(rig, tmp_path)
    update, read = rig.client.update_issue, rig.client.read_issue
    baseline = sum("mutation UpdateIssue" in q["query"] for q, _ in rig.provider.requests)

    def failed_readback(*_):
        raise LinearError("authored_readback_loss", uncertain=True)

    def with_loss(*args):
        response = update(*args)
        if loss == "response":
            raise LinearError("authored_response_loss", uncertain=True)
        if loss == "readback":
            monkeypatch.setattr(rig.client, "read_issue", failed_readback)
        return response

    monkeypatch.setattr(rig.client, "update_issue", with_loss)
    result = human_action(service, observations, task, target, "human-http-hold")
    held = rig.current(task)
    assert held.human_hold and held.decision_revision == 1
    assert rig.provider.issues[task.issue_id]["state"]["name"] == target
    assert (
        sum("mutation UpdateIssue" in q["query"] for q, _ in rig.provider.requests) == baseline + 1
    )
    if loss is None:
        assert result["ok"] and held.observed_status == target
        headers, body = rig.signed(rig.provider.event(held.issue_id))
        assert rig.receiver.receive("POST", headers, body).status == 200
        assert rig.inbound.run_once(rig.handle)["classification"] == "outbound_echo"
        assert rig.current(held) == held
        released = human_action(service, observations, held, "Todo", "explicit-human-release")
        assert released["ok"] and released["data"]["human_hold"] is False
        assert released["data"]["decision_revision"] == 2
        assert rig.provider.issues[held.issue_id]["state"]["name"] == "Todo"
        return
    assert result["outcome"] == "uncertain"
    operation = rig.core.sync.list_operations(rig.handle)[-1]
    assert operation.state == "uncertain"
    monkeypatch.setattr(rig.client, "read_issue", read)
    monkeypatch.setattr(rig.client, "update_issue", update)
    # Reopen Core and the actual authority journal before supported read-only recovery.
    rig.core = rig.new_core()
    rig.outbound = OutboundDrain(rig.core, rig.client, rig.journal)
    human_service(rig, tmp_path)
    recovery = Reconciliation(rig.core, rig.client, rig.journal)
    if loss == "response":
        with pytest.raises(LinearError, match="creation_identity_uncertain"):
            recovery.recover_outbound(rig.handle, operation.id)
        assert rig.current(held).human_hold
    else:
        recovered = recovery.recover_outbound(rig.handle, operation.id)
        assert recovered.human_hold and recovered.observed_status == target
        assert recovered.decision_revision == 1
    assert rig.outbound.run_once(rig.handle) is None
    assert (
        sum("mutation UpdateIssue" in q["query"] for q, _ in rig.provider.requests) == baseline + 1
    )


@pytest.mark.parametrize("phase", ["publish", "recover"])
@pytest.mark.parametrize(
    "fault", ["missing", "foreign", "stale", "actor", "expired", "transition_actor"]
)
def test_hb10_hold_destination_without_exact_human_proof_cannot_publish(
    rig, tmp_path, monkeypatch, fault, phase
):
    from dataclasses import replace

    from ai_task_manager.domains.work.errors import EvidenceRequired

    task = rig.queued()
    service, observations = human_service(rig, tmp_path, provider=False)
    pending = human_action(service, observations, task, "Backlog", "pending-held-write")
    assert pending["ok"] and pending["outcome"] == "pending"
    held = rig.current(task)
    operation = rig.core.sync.list_operations(rig.handle)[-1]
    if phase == "recover":
        from ai_task_manager.adapters.linear.errors import LinearError

        actual_update, actual_read = rig.client.update_issue, rig.client.read_issue

        def lose_readback(*args):
            result = actual_update(*args)

            def failed_read(*_):
                raise LinearError("authored_readback_loss", uncertain=True)

            monkeypatch.setattr(rig.client, "read_issue", failed_read)
            return result

        monkeypatch.setattr(rig.client, "update_issue", lose_readback)
        assert rig.outbound.run_once(rig.handle)["state"] == "uncertain"
        monkeypatch.setattr(rig.client, "update_issue", actual_update)
        monkeypatch.setattr(rig.client, "read_issue", actual_read)
    original = rig.core.context.evidence.verify_evidence
    selected = (
        operation.payload["decision_ref"] if fault == "transition_actor" else held.decision_ref
    )

    def verify(ref):
        proof = original(ref)
        if ref != selected:
            return proof
        if fault == "missing":
            raise EvidenceRequired("Authored missing human authority")
        if fault == "foreign":
            return replace(proof, scope=replace(rig.scope, principal="foreign"))
        if fault == "expired":
            return replace(proof, expires_at=rig.now)
        return replace(
            proof,
            payload={
                **proof.payload,
                **(
                    {"previous_revision": held.decision_revision}
                    if fault == "stale"
                    else {"actor": "agent"}
                ),
            },
        )

    monkeypatch.setattr(rig.core.context.evidence, "verify_evidence", verify)
    before = len(rig.provider.requests)
    if phase == "publish":
        result = rig.outbound.run_once(rig.handle)
        assert result["state"] == "failed"
        assert len(rig.provider.requests) == before
        assert rig.provider.issues[held.issue_id]["state"]["name"] == "Todo"
    else:
        with pytest.raises((EvidenceRequired, LinearError)):
            rig.reconcile.recover_outbound(rig.handle, operation.id)
        assert all(
            not q["query"].lstrip().startswith("mutation")
            for q, _ in rig.provider.requests[before:]
        )
        assert rig.core.sync.list_operations(rig.handle)[-1].state == "uncertain"
    assert rig.current(held).human_hold


def test_hb11_newer_hold_fences_old_publication_and_nonhold_work(rig, tmp_path):
    task = rig.queued()
    service, observations = human_service(rig, tmp_path, provider=False)
    assert human_action(service, observations, task, "Backlog", "first-human-hold")["ok"]
    held = rig.current(task)
    ref = rig.proof(
        "human_decision", held.id, {"previous_revision": held.decision_revision, "hold": True}
    )
    newer = rig.core.decisions.record(rig.handle, held.id, held.version, ref, rig.key())
    before = len(rig.provider.requests)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "failed" and result["error"] == "stale_outbound_authority"
    assert len(rig.provider.requests) == before and rig.current(newer) == newer
