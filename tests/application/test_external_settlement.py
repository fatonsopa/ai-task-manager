"""Existing signed human observation settlement through the fixed public surface."""

from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.application.service.models import SCHEMAS
from tests.application.helpers import Observations
from tests.runtime.helpers import RuntimeRig


@pytest.fixture
def env(tmp_path):
    value = RuntimeRig(tmp_path)
    try:
        yield value
    finally:
        value.close()


def observed_service(rr):
    from ai_task_manager.adapters.decisions.authority import DecisionAuthority
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal

    r = rr.rig
    journal = AuthorityJournal(
        rr.path / "authority.sqlite", rr.path / "authority.key", clock=lambda: r.now
    )
    observations = Observations()
    observations.grants["grant"] = HostGrant(
        r.scope, r.scope.logical_session, ("hold",), "grant", r.now + 120
    )
    decisions = DecisionAuthority(journal, observations)
    old = r.core.context.evidence
    r.core.context.evidence = EvidenceRouter(
        [
            *old.routes,
            EvidenceRoute("external-fixture:", frozenset({"external_observation"}), r.proofs),
        ]
    )
    snapshot = SimpleNamespace(
        id="canonical-owned",
        revision="remote:1",
        status="On Hold",
        title="",
        body="",
        assignee_id=r.scope.principal,
    )
    client = SimpleNamespace(
        read_issue=lambda _: snapshot,
        check_connection=lambda *args, **kwargs: None,
        require_delegate=lambda _: None,
    )
    service = ApplicationService(
        r.core,
        r.handle,
        decisions=decisions,
        grant_ref="grant",
        inbound=SimpleNamespace(client=client),
    )
    return service, observations, snapshot


def stage(rr, service, snapshot, task):
    r = rr.rig
    snapshot.id = task.issue_id
    snapshot.title = task.title
    snapshot.body = task.body
    ref = "external-fixture:" + r.key()
    from ai_task_manager.domains.work.models import Evidence

    r.proofs.evidence[ref] = Evidence(
        "external_observation",
        r.scope,
        task.id,
        {
            "issue_id": task.issue_id,
            "status": snapshot.status,
            "source_revision": snapshot.revision,
            "title": task.title,
            "body": task.body,
            "actor_kind": "human",
            "actor_authorized": True,
            "assigned_to_owner": True,
            "reason": "Authored hold",
        },
    )
    result = r.core.external.observe(
        r.handle, task.id, task.version, task.decision_revision, ref, r.key()
    )
    return result["observation_id"]


def request(task, observation, key="settle"):
    return {
        "task_id": task.id,
        "observation_id": observation,
        "expected_version": task.version,
        "decision_revision": task.decision_revision,
        "cause": "hold",
        "operation_key": key,
    }


def test_real_owned_stop_precedes_public_settlement_and_lost_reply_recovery(env, monkeypatch):
    rr = env
    r = rr.rig
    dispatch = rr.start()
    rr.admit(dispatch)
    service, observations, snapshot = observed_service(rr)
    task = r.core.get_task(r.handle, r.core.get_attempt(r.handle, dispatch.attempt_id).task_id)
    observation = stage(rr, service, snapshot, task)
    fenced = r.current(task)
    assert fenced.human_hold and fenced.execution_state == "running"
    denied = service.call("task_settle_external", request(fenced, observation, "before-stop"))
    assert not denied["ok"] and dispatch.worker.process.poll() is None
    rr.supervisor.stop(r.handle, dispatch, operation_key=r.key(), reason="Authored hold")
    assert dispatch.worker.process.returncode is not None
    current = r.current(task)
    original_invoke = service._invoke

    def lost_reply(*args):
        original_invoke(*args)
        raise OSError("Authored lost service reply after Core settlement")

    monkeypatch.setattr(service, "_invoke", lost_reply)
    assert (
        service.call("task_settle_external", request(current, observation))["outcome"] == "failed"
    )
    monkeypatch.setattr(service, "_invoke", original_invoke)
    snapshot.revision = "later-remote-revision"
    # A committed retry reports current local facts without another provider effect/read.
    result = service.call("task_settle_external", request(current, observation))
    assert result["ok"] and result["data"]["observed_status"] == "On Hold", result
    assert r.core.external.observations(r.handle, task.id)[0]["state"] == "applied"
    with r.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM resource_leases").fetchone()[0] == 0
    assert "task_settle_external" in SCHEMAS


@pytest.mark.parametrize(
    "change", ["readback", "revoked", "unauthorized_actor", "missing_delivery"]
)
def test_current_readback_authority_actor_and_destination_evidence_refuse(env, change):
    rr = env
    r = rr.rig
    if change == "missing_delivery":
        from tests.domains.test_review_delivery import review

        # Reuse declared policy preconditions; this case establishes no deployment or worker stop.
        original = r.core.context.evidence
        r.core.context.evidence = r.proofs
        try:
            task = review(r)
        finally:
            r.core.context.evidence = original
    else:
        task = r.queued()
    service, observations, snapshot = observed_service(rr)
    if change == "missing_delivery":
        snapshot.status = "In Dev"
    observation = stage(rr, service, snapshot, task)
    current = r.current(task)
    if change == "readback":
        snapshot.revision = "remote:2"
    elif change == "revoked":

        def revoke_during_read(_):
            observations.grants.clear()
            return snapshot

        service.inbound.client.read_issue = revoke_during_read
    elif change == "unauthorized_actor":
        # Use the actual Core's unknown-actor observation path; never edit persisted rows.
        snapshot.revision = "remote:2"
        snapshot.status = "Done"
        ref = "external-fixture:" + r.key()
        from ai_task_manager.domains.work.models import Evidence

        r.proofs.evidence[ref] = Evidence(
            "external_observation",
            r.scope,
            task.id,
            {
                "issue_id": task.issue_id,
                "status": "Done",
                "source_revision": "remote:2",
                "title": task.title,
                "body": task.body,
                "actor_kind": "integration",
                "actor_authorized": False,
                "assigned_to_owner": True,
            },
        )
        observation = r.core.external.observe(
            r.handle, task.id, current.version, current.decision_revision, ref, r.key()
        )["observation_id"]
        current = r.current(task)
    data = request(current, observation)
    if change == "missing_delivery":
        data["cause"] = "review_decision"
    elif change == "unauthorized_actor":
        data["cause"] = "release"
    assert not service.call("task_settle_external", data)["ok"]
    assert r.current(task) == current
