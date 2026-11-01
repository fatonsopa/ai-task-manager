import pytest

from ai_task_manager.adapters.proofs.store import RuntimeProofs
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.supervisor.models import RuntimeRefused


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_model_named_harness_cannot_create_in_progress_or_model_lease(rr, host):
    r = rr.rig
    routes = r.core.context.evidence
    r.capacity = CapacityPolicy(host, 2, 2)
    r.core = r.new_core()
    r.core.context.evidence = routes
    rr.supervisor.core = r.core
    task = r.queued()
    with pytest.raises((EvidenceRequired, RuntimeRefused)):
        rr.start(task, host=host)
    current = r.core.get_task(r.handle, task.id)
    assert current.observed_status == "Todo" and current.desired_status == "Todo"
    assert all(
        op.payload["desired_status"] != "In Progress"
        for op in r.core.sync.list_operations(r.handle)
    )
    assert not rr.journal.observations(next(iter(rr.runtime.workers)), "model_worker_ready")


def test_unconfigured_proof_store_cannot_promote_harness_to_worker(rr):
    dispatch = rr.start()
    unconfigured = RuntimeProofs(rr.journal, rr.path / "unconfigured-proof.key")
    with pytest.raises((EvidenceRequired, RuntimeRefused)):
        unconfigured.startup(rr.rig.scope, dispatch.attempt_id, dispatch.worker.observation_id)


@pytest.mark.parametrize("host", ["claude", "codex", "claude_roni"])
def test_model_profile_cannot_be_declared_as_a_fixture_host(rr, host):
    with pytest.raises(RuntimeRefused):
        RuntimeProofs(rr.journal, rr.path / "invalid-fixture-key", fixture_hosts={host})
