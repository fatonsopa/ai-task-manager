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


def test_imported_workflow_port_uses_current_start_readbacks(rig, provider, journal, tmp_path, monkeypatch):
    """Actual imported signed source/Core/ManagedWorkers; stop at reservation ownership."""
    import json
    from types import SimpleNamespace
    from ai_task_manager.adapters.workflows.runtime import ManagedWorkerPort
    from ai_task_manager.domains.work.models import CapacityPolicy
    from ai_task_manager.storage import capacity
    from ai_task_manager.supervisor.managed import ManagedWorkers

    class Reserved(Exception):
        pass

    issue = provider.seed("Todo")
    task = rig.queue(rig.admit(issue))
    with rig.db.transaction() as conn:
        capacity.configure(conn, CapacityPolicy("claude", 2, 2))
    runtime = RuntimeJournal(tmp_path / "imported-runtime.sqlite")
    claims = []

    def claim(attempt, scope, *, deadline):
        claims.append(attempt)
        raise Reserved()

    def forbidden(*args, **kwargs):
        raise AssertionError("No resource/native effect belongs to this reservation control")

    monkeypatch.setattr(runtime, "claim", claim)
    workers = ManagedWorkers(
        rig.core, runtime, SimpleNamespace(prepare=forbidden),
        SimpleNamespace(launch_waiting=forbidden), None,
        SimpleNamespace(confirm_pending=forbidden),
        SimpleNamespace(check_admission=lambda host: None, journal=journal, launch=forbidden),
        deadline=time.time() + 90, principal_ref="authored-existing-principal",
    )
    port = ManagedWorkerPort(workers)
    port.existing_readbacks = rig.intake.worker_readbacks  # Trusted composition input.
    failure = None
    try:
        port.start(rig.handle, task, SimpleNamespace(host="claude"),
                   rig.decision(task, "In Progress"), "imported-workflow-start")
    except Reserved:
        pass
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    facts = {"failure": failure, "claims": [{"id": a.id, "state": a.state} for a in claims],
             "imported": rig.core.existing.is_imported(rig.handle, task.id),
             "task_version": task.version,
             "mutations": sum("mutation " in q["query"] for q, _ in provider.requests)}
    (tmp_path / "imported-start-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
    assert failure is None and len(claims) == 1 and claims[0].state == "reserved", facts
    assert facts["mutations"] == 0


@pytest.mark.parametrize("changed", [False, True])
def test_imported_workflow_managed_start_rechecks_signed_source(
    composition, provider, tmp_path, changed
):
    """Reuse the actual scripted native graph, with real imported-source proof routes."""
    import json
    from dataclasses import asdict
    from ai_task_manager.adapters.workflows.runtime import ManagedWorkerPort
    from tests.existing.helpers import ExistingRig
    from tests.linear.helpers import uid

    r, _, _, workers, factory, _, accounting = composition()
    imported = ExistingRig(tmp_path / "imported-core", provider, db=r.rig.db)
    task = imported.queue(imported.admit(provider.seed("Todo")))
    imported.core.context.evidence = EvidenceRouter([
        *imported.router.routes,
        EvidenceRoute("runtime-proof:", RUNTIME_KINDS, workers.proofs),
    ])
    workers.core = factory.core = imported.core
    workers.provider = OutboundDrain(imported.core, imported.client, imported.journal)
    contexts = []

    def factory_readbacks(task_id, key):
        actual = imported.intake.worker_readbacks(task_id, key)

        def readback(**context):
            contexts.append(context)
            if changed and context["phase"] == "start":
                provider.issues[task.issue_id]["assignee"] = {"id": uid(999)}
            ref = actual(**context)
            proof = imported.journal.verify_evidence(ref)
            assert proof.kind == "existing_issue_readback"
            assert proof.payload["context"] == context | {"purpose": "start", "discovery_ref": None}
            return ref
        return readback

    port = ManagedWorkerPort(workers, existing_readbacks=factory_readbacks)
    attempt, error = None, None
    try:
        attempt = port.start(imported.handle, task, type("Plan", (), {"host": "codex"})(),
                             imported.decision(task, "In Progress"), "imported-native-start")
    except Exception as failure:
        error = {"type": type(failure).__name__, "message": str(failure)}
    finally:
        if attempt is not None:
            workers.stop(imported.handle, attempt.id, operation_key="imported-native-stop")
    current = imported.core.get_task(imported.handle, task.id)
    facts = {"changed": changed, "contexts": contexts, "error": error,
             "attempt": asdict(attempt) if attempt else None,
             "current": asdict(current), "accounted_assignment_turns": accounting.outcomes,
             "stopped": [native.process._closed for native in factory.owned.values()]}
    (tmp_path / "imported-managed-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
    assert [row["phase"] for row in contexts] == ["reserve", "start"], facts
    assert all(row["operation_key"] == "imported-native-start:" + row["phase"] for row in contexts)
    assert not accounting.outcomes and all(facts["stopped"]), facts
    if changed:
        assert error is not None and current.execution_state == "failed", facts
    else:
        assert error is None and attempt.state == "running", facts
