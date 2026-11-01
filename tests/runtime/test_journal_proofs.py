import json
from dataclasses import asdict, replace

import pytest

from ai_task_manager.adapters.process.runtime import ProcessRuntime
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Evidence
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeUncertain


def test_deadline_and_launch_identity_survive_restart(rr):
    dispatch = rr.start()
    original = rr.journal.attempt(dispatch.attempt_id)
    restarted = RuntimeJournal(rr.journal.path, clock=lambda: rr.rig.now)
    assert restarted.attempt(dispatch.attempt_id) == original
    other = ProcessRuntime(restarted, rr.path / "restarted-output")
    assert other.reconcile(dispatch.attempt_id)["state"] == "unknown"
    with pytest.raises(RuntimeUncertain):
        other.launch_waiting(dispatch.attempt_id)
    assert dispatch.worker.group.observe().live_members


def test_settled_runtime_recovery_uses_observed_outcome(rr):
    dispatch = rr.start()
    result = rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    other = ProcessRuntime(rr.journal, rr.path / "restarted-output")
    assert other.reconcile(dispatch.attempt_id) == {
        "state": "quiescent",
        "observation_id": result.observation_id,
    }


def test_registered_intent_prevents_competing_effect_after_lost_reply(rr):
    task, attempt = rr.rig.reserved()
    owner = rr.journal.claim(attempt, asdict(rr.rig.scope), deadline=rr.rig.now + 30)
    rr.journal.intend(attempt.id, "launch", {"kind": "waiting_harness"})
    restarted = RuntimeJournal(rr.journal.path, clock=lambda: rr.rig.now)
    with pytest.raises(RuntimeUncertain):
        restarted.claim(attempt, asdict(rr.rig.scope), deadline=rr.rig.now + 100)
    assert restarted.attempt(attempt.id) == owner
    assert not rr.runtime.workers


@pytest.mark.parametrize("deadline", [0, 999, float("nan"), float("inf")])
def test_invalid_deadline_is_refused_before_effect(rr, deadline):
    task, attempt = rr.rig.reserved()
    with pytest.raises(RuntimeRefused):
        rr.journal.claim(attempt, asdict(rr.rig.scope), deadline=deadline)
    assert not rr.runtime.workers


def test_receipts_survive_restart_without_exposing_signing_material(rr):
    dispatch = rr.start()
    ref = rr.proofs.startup(rr.rig.scope, dispatch.attempt_id, dispatch.worker.observation_id)
    restarted = RuntimeProofs(
        rr.journal, rr.path / "controller-only/runtime.key", fixture_hosts={"fixture"}
    )
    assert restarted.verify_evidence(ref) == rr.proofs.verify_evidence(ref)
    assert not hasattr(rr.proofs, "issue")
    grant = rr.journal.effect(dispatch.attempt_id, "launch")
    assert "key" not in json.dumps(grant)
    assert rr.proofs._key not in dispatch.worker.stdout.read_bytes()
    assert rr.proofs._key not in dispatch.worker.stderr.read_bytes()
    assert not list(dispatch.resources.worktree.glob("**/runtime.key"))


@pytest.mark.parametrize(
    "mutation", ["expiry", "receipt", "source", "cross_scope", "cross_attempt"]
)
def test_changed_stale_or_cross_scope_receipts_refuse(rr, mutation):
    dispatch = rr.start()
    ref = rr.proofs.startup(rr.rig.scope, dispatch.attempt_id, dispatch.worker.observation_id)
    if mutation == "expiry":
        rr.rig.now += 61
    elif mutation == "receipt":
        with rr.journal.transaction() as conn:
            row = conn.execute("SELECT data FROM runtime_receipts WHERE ref=?", (ref,)).fetchone()
            data = json.loads(row[0])
            data["payload"]["worker_id"] = "forged"
            conn.execute("UPDATE runtime_receipts SET data=? WHERE ref=?", (json.dumps(data), ref))
    elif mutation == "source":
        with rr.journal.transaction() as conn:
            conn.execute(
                "UPDATE runtime_observations SET data='{}' WHERE id=?",
                (dispatch.worker.observation_id,),
            )
    elif mutation == "cross_scope":
        with pytest.raises(EvidenceRequired):
            rr.proofs.startup(
                replace(rr.rig.scope, logical_session="other"),
                dispatch.attempt_id,
                dispatch.worker.observation_id,
            )
        return
    else:
        second = rr.start()
        with pytest.raises(RuntimeRefused):
            rr.proofs.startup(rr.rig.scope, second.attempt_id, dispatch.worker.observation_id)
        return
    with pytest.raises(EvidenceRequired):
        rr.proofs.verify_evidence(ref)


def test_proof_kinds_cannot_be_issued_from_different_observations(rr):
    dispatch = rr.start()
    with pytest.raises(RuntimeRefused):
        rr.proofs.outcome(rr.rig.scope, dispatch.attempt_id, dispatch.worker.observation_id)
    with pytest.raises(RuntimeRefused):
        rr.proofs.startup(rr.rig.scope, dispatch.attempt_id, dispatch.resources.observation_id)


class Verifier:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = 0

    def verify_evidence(self, ref):
        self.calls += 1
        return self.evidence


@pytest.mark.parametrize("other", ["runtime-proof:", "runtime-proof:sub:", "other:"])
def test_router_rejects_duplicate_issuer_or_overlapping_kind(rr, other):
    with pytest.raises(EvidenceRequired):
        EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
                EvidenceRoute(other, frozenset({"worker_start"}), rr.proofs),
            ]
        )


def test_router_has_no_unknown_prefix_or_wrong_kind_fallback(rr):
    wrong = Verifier(Evidence("provider_status", rr.rig.scope, "task", {}))
    unused = Verifier(Evidence("worker_start", rr.rig.scope, "attempt", {}))
    router = EvidenceRouter(
        [
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, wrong),
            EvidenceRoute("provider-proof:", frozenset({"provider_status"}), unused),
        ]
    )
    with pytest.raises(EvidenceRequired):
        router.verify_evidence("unknown:forged")
    with pytest.raises(EvidenceRequired):
        router.verify_evidence("runtime-proof:forged")
    assert wrong.calls == 1 and unused.calls == 0


def test_runtime_store_rejects_foreign_reference(rr):
    with pytest.raises(EvidenceRequired):
        rr.proofs.verify_evidence("linear-proof:forged")
