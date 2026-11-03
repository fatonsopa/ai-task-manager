"""Real dependency reader/Core starts; stop before runtime, resource or native effects."""

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins
from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.executions import ModelWork, ReviewedExecutions
from ai_task_manager.adapters.hosts.bridge.policy import NativeProfile
from ai_task_manager.adapters.linear.models import WireResponse
from ai_task_manager.adapters.workflow_reviews.capture_store import CaptureStore
from ai_task_manager.adapters.workflow_reviews.models import Inspection
from ai_task_manager.adapters.workflow_reviews.native import NativeReviewCapture, review_prompt
from ai_task_manager.adapters.workflows.runtime import ManagedWorkerPort
from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    EvidenceRequired,
    StaleDecision,
    StaleVersion,
)
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.installation.workflows.registry import NativeRegistration
from ai_task_manager.storage import capacity
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.managed import ManagedWorkers
from tests.scheduling.provider_fixture import ProviderFixture


class Reserved(Exception):
    """Fixture boundary after actual Core reservation, before runtime ownership."""


def forbidden(*args, **kwargs):
    raise AssertionError("Runtime, resource and native effects are outside this case")


@pytest.fixture
def graph(tmp_path, monkeypatch):
    provider = ProviderFixture(tmp_path / "provider")
    rig = provider.rig
    task = provider.task()
    task = rig.transition(task, "Todo", "enqueue")
    provider.sync(task)
    with rig.db.transaction() as conn:
        capacity.configure(conn, CapacityPolicy("claude", 2, 2))
    journal = RuntimeJournal(tmp_path / "runtime.sqlite", clock=lambda: rig.now)
    claimed = []

    def claim(attempt, scope, *, deadline):
        claimed.append(attempt)
        raise Reserved()

    monkeypatch.setattr(journal, "claim", claim)
    workers = ManagedWorkers(
        rig.core,
        journal,
        SimpleNamespace(prepare=forbidden),
        SimpleNamespace(launch_waiting=forbidden),
        None,
        SimpleNamespace(confirm_pending=forbidden),
        SimpleNamespace(check_admission=lambda host: None, launch=forbidden),
        deadline=rig.now + 120,
        principal_ref="authored-principal",
        dependencies=provider.source,
    )
    return SimpleNamespace(
        provider=provider, r=rig, task=task, workers=workers, journal=journal, claimed=claimed
    )


def start(g, **changes):
    values = {
        "expected_version": g.task.version,
        "decision_revision": g.task.decision_revision,
        "decision_ref": g.r.decision(g.task, "In Progress"),
        "operation_key": "initial-managed-start",
    }
    values.update(changes)
    return g.workers.start(g.r.handle, g.task.id, "claude", **values)


def no_execution(g):
    with g.r.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM resource_leases").fetchone()[0] == 0
    with g.journal.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM runtime_attempts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM runtime_effects").fetchone()[0] == 0
    assert g.r.current(g.task).observed_status == "Todo"
    assert not g.claimed


def test_ir01_workflow_port_collects_initial_unknown_readiness_before_reserve(graph):
    g = graph
    assert g.r.core.scheduling.explain(g.r.handle, g.task.id)["state"] == "unknown"
    before = len(g.provider.calls)
    with pytest.raises(Reserved):
        ManagedWorkerPort(g.workers).start(
            g.r.handle,
            g.task,
            SimpleNamespace(host="claude"),
            g.r.decision(g.task, "In Progress"),
            "workflow-start",
        )
    assert len(g.provider.calls) > before
    assert len(g.claimed) == 1 and g.claimed[0].state == "reserved"
    events = g.r.core.notifications.list_events(g.r.handle)
    observed = [e.sequence for e in events if e.kind == "scheduling_snapshot_recorded"]
    reserved = [e.sequence for e in events if e.kind == "attempt_reserved"]
    assert observed and max(observed) < min(reserved)
    assert g.r.current(g.task).observed_status == "Todo"


@pytest.mark.parametrize("condition", ["unknown", "blocked", "changed", "connection"])
def test_ir02_current_provider_authority_must_allow_reservation(graph, monkeypatch, condition):
    g, p = graph, graph.provider
    if condition == "unknown":
        p.nodes[g.task.issue_id].pop("archivedAt")
    elif condition in {"blocked", "changed"}:
        if condition == "changed":
            assert p.refresh(g.task)["state"] == "ready"
        predecessor = p.task()
        p.edge(predecessor, g.task)
    else:
        send = p.send

        def changed_connection(query, variables, credentials):
            response = send(query, variables, credentials)
            if "VerifyBinding" in query:
                value = json.loads(response.body)
                value["data"]["viewer"]["id"] = str(uuid4())
                return WireResponse(200, {}, json.dumps(value).encode())
            return response

        monkeypatch.setattr(p, "send", changed_connection)
    with pytest.raises(SchedulingBlocked):
        start(g)
    assert g.r.core.scheduling.explain(g.r.handle, g.task.id)["state"] == (
        "unknown" if condition in {"unknown", "connection"} else "blocked"
    )
    no_execution(g)


@pytest.mark.parametrize("field", ["version", "revision", "source_session", "source_owner"])
def test_ir03_changed_request_or_source_refuses_before_provider_reads(graph, field):
    g = graph
    values = {}
    if field == "version":
        values["expected_version"] = g.task.version - 1
    elif field == "revision":
        values["decision_revision"] = g.task.decision_revision + 1
    elif field == "source_session":
        other = g.r.session(replace(g.r.scope, logical_session="another-session"))
        g.provider.source.binding = g.r.core.session_binding(other)
    else:
        g.workers.authorities.dependencies = object()
    before = len(g.provider.calls)
    with pytest.raises((AccessDenied, StaleVersion)):
        start(g, **values)
    assert len(g.provider.calls) == before
    no_execution(g)


def test_ir04_wrong_decision_cannot_reserve_after_collection(graph):
    g = graph
    with pytest.raises(StaleDecision):
        start(g, decision_ref=g.r.decision(g.task, "Done"))
    no_execution(g)


def test_ir05_reserve_refusal_has_no_runtime_effect_and_retry_recollects(graph, monkeypatch):
    g = graph
    reserve = g.r.core.execution.reserve
    calls = []

    def refuse_once(*args, **kwargs):
        assert g.r.core.scheduling.explain(g.r.handle, g.task.id)["state"] == "ready"
        calls.append(True)
        if len(calls) == 1:
            raise EvidenceRequired("Authored pre-reservation refusal")
        return reserve(*args, **kwargs)

    monkeypatch.setattr(g.r.core.execution, "reserve", refuse_once)
    with pytest.raises(EvidenceRequired, match="pre-reservation"):
        start(g)
    no_execution(g)
    before = len(g.provider.calls)
    g.r.now += 31
    with pytest.raises(Reserved):
        start(g)
    assert len(g.provider.calls) > before and len(g.claimed) == 1 and len(calls) == 2


def test_ir06_collection_cannot_extend_original_deadline(graph):
    g = graph
    g.workers.deadline = g.r.now + 1
    crossed = []

    def cross_deadline(query, variables):
        if "DependencySchema" in query and not crossed:
            crossed.append(True)
            g.r.now += 2

    g.provider.callback = cross_deadline
    with pytest.raises(AccessDenied, match="deadline expired"):
        start(g)
    assert crossed
    no_execution(g)


def test_ir07_native_review_capture_uses_same_initial_collection(graph, tmp_path):
    g = graph
    profile = NativeProfile("authored-review-no-model", "high")
    account = "a" * 64
    pins = NativeSourcePins({__file__: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    registration = NativeRegistration(
        "authored-review-registration", "claude", profile, account, pins, forbidden
    )
    factory = ManagedHostFactory(
        g.r.core,
        g.workers.resources,
        None,
        tmp_path / "native",
        forbidden,
        SimpleNamespace(check_admission=lambda host: None),
        {"claude": account},
        model_profiles={"claude": profile},
    )
    g.workers.hosts = factory
    inspection = Inspection(
        json.dumps(
            {
                "scope": asdict(g.r.scope),
                "task_id": "distinct-author-task",
                "author": {"worker_id": "distinct-author-worker", "attempt_id": "author-attempt"},
                "inspection": {"artifact_facts": {}},
                "criteria": [["criterion", "Authored request"]],
            }
        ),
        (),
    )
    g.workers.executions = ReviewedExecutions(
        None,
        {"review": ModelWork(g.task.id, "claude", review_prompt(inspection), "unused")},
        None,
        None,
    )
    capture = NativeReviewCapture(
        g.workers,
        g.r.handle,
        g.task.id,
        registration,
        "review",
        lambda scope, task, target, action: g.r.decision(task, target),
    )
    store = CaptureStore(tmp_path / "captures")
    before = len(g.provider.calls)
    assert g.r.core.scheduling.explain(g.r.handle, g.task.id)["state"] == "unknown"
    with pytest.raises(Reserved):
        capture.capture(inspection, store)
    assert len(g.provider.calls) > before and len(g.claimed) == 1
    assert not factory.owned
    assert (store.directory(inspection) / "intent.json").exists()
    assert (store.directory(inspection) / "failure.json").exists()
    assert not (store.directory(inspection) / "capture.json").exists()
