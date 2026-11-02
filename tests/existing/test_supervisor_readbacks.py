"""Actual local processes and authored native controls; no live model acceptance."""

import subprocess
import time

import pytest

from ai_task_manager.adapters.process.runtime import ProcessRuntime
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.adapters.resources.manager import ResourceManager
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS
from ai_task_manager.supervisor.accounting import MechanicalCommands
from ai_task_manager.supervisor.dispatch import WorkerSupervisor
from ai_task_manager.supervisor.journal import RuntimeJournal
from tests.hosts import conftest as host_fixtures
from tests.hosts import test_composition as host_composition
from tests.runtime.helpers import FixtureReleasePolicy

# Reuse the bridge owner's actual object graph and honestly synthetic native CLI.
journal = host_fixtures.journal
composition = host_composition.composition


@pytest.mark.parametrize("failure", [False, True])
def test_ea77_managed_port_passes_exact_context_and_aborts_after_init_failure(
    composition, monkeypatch, failure
):
    value = composition()
    r, task, decision, workers, factory, provider, accounting = value
    calls = []
    reserve, start = r.rig.core.execution.reserve, r.rig.core.execution.start

    def checked_reserve(*args, **kwargs):
        assert calls[-1]["phase"] == "reserve"
        assert kwargs["readback_ref"] == "synthetic-port-reserve"
        assert calls[-1]["expected_version"] == args[3]
        assert calls[-1]["decision_revision"] == args[4]
        assert calls[-1]["operation_key"] == args[6]
        return reserve(*args, **kwargs)

    def checked_start(*args, **kwargs):
        assert calls[-1]["phase"] == "start"
        assert kwargs["readback_ref"] == "synthetic-port-start"
        assert calls[-1]["attempt_id"] == args[1]
        assert calls[-1]["expected_version"] == args[2]
        assert calls[-1]["decision_revision"] == args[3]
        assert calls[-1]["operation_key"] == args[6]
        return start(*args, **kwargs)

    monkeypatch.setattr(r.rig.core.execution, "reserve", checked_reserve)
    monkeypatch.setattr(r.rig.core.execution, "start", checked_start)

    def source_readbacks(**context):
        calls.append(context)
        assert context["task_id"] == task.id
        assert context["operation_key"] == "port-test:" + context["phase"]
        if context["phase"] == "start":
            assert factory.owned  # Actual scripted CLI initialized; no assigned prompt yet.
            assert not accounting.outcomes
            if failure:
                raise EvidenceRequired("Injected complete-readback refusal after initialization")
        return "synthetic-port-" + context["phase"]

    arguments = dict(
        expected_version=task.version,
        decision_revision=task.decision_revision,
        decision_ref=decision,
        operation_key="port-test",
        source_readbacks=source_readbacks,
    )
    if failure:
        with pytest.raises(EvidenceRequired, match="after initialization"):
            workers.start(r.rig.handle, task.id, "codex", **arguments)
        assert not provider.calls and not accounting.outcomes
        assert r.rig.core.get_task(r.rig.handle, task.id).execution_state == "failed"
        assert all(native.process._closed for native in factory.owned.values())
    else:
        attempt = workers.start(r.rig.handle, task.id, "codex", **arguments)
        assert attempt.state == "running"
    assert [row["phase"] for row in calls] == ["reserve", "start"]
    assert calls[0]["expected_version"] == task.version
    assert calls[0]["attempt_id"] is None
    assert calls[1]["expected_version"] > calls[0]["expected_version"]


@pytest.mark.parametrize("failure", [False, True])
def test_ea77_imported_task_actual_provider_and_fixture_process_use_separate_rereads(
    rig, provider, tmp_path, failure
):
    issue = provider.seed("Todo")
    task = rig.queue(rig.admit(issue))
    root = tmp_path / "fixture-runtime"
    root.mkdir(mode=0o700)
    repository = root / "source"
    repository.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("commit", "--allow-empty", "-qm", "Fixture baseline"),
    ):
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    journal = RuntimeJournal(root / "runtime.sqlite")
    proofs = RuntimeProofs(journal, root / "private/runtime.key", fixture_hosts={"fixture"})
    rig.core.context.evidence = EvidenceRouter(
        [
            *rig.router.routes,
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
        ]
    )
    resources = ResourceManager(journal, root / "owned", repository, metadata_paths=(rig.db.path,))
    resources.release_policy = FixtureReleasePolicy(root / "owned", repository)
    runtime = ProcessRuntime(journal, root / "outputs")
    supervisor = WorkerSupervisor(
        rig.core,
        journal,
        resources,
        runtime,
        proofs,
        OutboundDrain(rig.core, rig.client, rig.journal),
        MechanicalCommands(),
    )
    actual = rig.intake.worker_readbacks(task.id, "actual-source")
    observed = []

    def source_readbacks(**context):
        # A separate BEGIN IMMEDIATE would fail if the caller held a Core writer transaction.
        with rig.db.transaction() as conn:
            assert conn.in_transaction
        if failure and context["phase"] == "start":
            raise EvidenceRequired("Injected second source read refusal")
        ref = actual(**context)
        observed.append((context, rig.journal.verify_evidence(ref)))
        return ref

    dispatch = None
    try:
        arguments = dict(
            expected_version=task.version,
            decision_revision=task.decision_revision,
            decision_ref=rig.decision(task, "In Progress"),
            operation_key="actual-source",
            deadline=time.time() + 120,
            source_readbacks=source_readbacks,
        )
        if failure:
            with pytest.raises(EvidenceRequired, match="second source"):
                supervisor.start(rig.handle, task.id, "fixture", **arguments)
            assert not [
                request for request, _ in provider.requests if "mutation " in request["query"]
            ]
            assert rig.core.get_task(rig.handle, task.id).execution_state == "failed"
            assert all(worker.process.poll() is not None for worker in runtime.workers.values())
        else:
            dispatch = supervisor.start(rig.handle, task.id, "fixture", **arguments)
            running = supervisor.admit(rig.handle, dispatch, operation_key="fixture-admit")
            assert running.state == "running"
            assert len(observed) == 2 and observed[0][1].subject == observed[1][1].subject == issue
            assert [item[1].payload["context"]["phase"] for item in observed] == [
                "reserve",
                "start",
            ]
            assert all(item[1].kind == "existing_issue_readback" for item in observed)
            assert all(
                item[1].payload["context"] == item[0] | {"purpose": "start", "discovery_ref": None}
                for item in observed
            )
            supervisor.stop(rig.handle, dispatch, operation_key="fixture-stop")
        with rig.db.transaction() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM attempts")]
        for attempt_id in ids:
            attempt = rig.core.get_attempt(rig.handle, attempt_id)
            assert attempt.state not in ACTIVE_ATTEMPTS
            resources.cleanup(rig.core, rig.handle, attempt_id)
        assert not list((root / "owned/trees").glob("*"))
    finally:
        for worker in runtime.workers.values():
            if worker.process.poll() is None:
                runtime.stop(worker, reason="owned fixture cleanup")
