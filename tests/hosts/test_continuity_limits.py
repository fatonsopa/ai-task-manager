"""Virtual cap controls are authored; one process case adds actual liveness evidence."""

import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.records import (
    HostBinding,
    ManagedProcesses,
    ProcessIdentity,
)
from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope
from tests.hosts.accounting_fixtures import FixtureAccounting
from tests.hosts.test_authority_latency import graph as renewal_graph
from tests.hosts.test_composition import composition as composition


class ClockGuard:
    """Authored local guard port, not authenticated/native model evidence."""

    configuration = None

    @contextmanager
    def observation_boundary(self, **kwargs):
        yield

    @contextmanager
    def startup_current_boundary(self):
        yield


@pytest.fixture
def clock_native(tmp_path):
    root = tmp_path / "clock-control"
    root.mkdir(mode=0o700)
    now = [time.time()]
    journal = HostJournal(root / "journal.sqlite", root / "key", clock=lambda: now[0])
    binding = HostBinding(
        Scope("linear", "workspace", "principal", "project", "logical"),
        "session",
        "attempt",
        "worker",
        "codex",
        "authored-principal",
        "a" * 64,
        now[0] + 100,
    )
    policy = SimpleNamespace(
        model="authored-model",
        effort="high",
        configuration_digest=binding.configuration_digest,
        prepare_admission=lambda: None,
        check_admission_metadata=lambda value: None,
    )
    accounting = FixtureAccounting(root / "accounting")
    invocation = accounting.initialize(policy, binding)
    identity = ProcessIdentity(12345, "authored-clock-only", 12345)
    process = SimpleNamespace(
        identity=identity,
        native_exit=object(),
        _closed=False,
        deadline=invocation.native.binding.deadline,
        invocation=invocation,
        _bound_invocation=invocation,
        check_current_liveness=lambda: None,
    )
    guard = ClockGuard()
    ready = journal._append(
        "startup",
        {
            **asdict(binding),
            "process_id": identity.pid,
            "process_start_identity": identity.start_identity,
            "session_id": "authored-session",
        },
        expires_at=now[0] + 10,
    )
    startup = SimpleNamespace(
        binding=binding,
        policy=policy,
        configuration=None,
        model_guard=guard,
        ready_ref=ready,
        native_session="authored-session",
    )
    registry = ManagedProcesses()
    registry.attach(binding.attempt_id, process)
    native = SimpleNamespace(
        binding=binding,
        process=process,
        startup=startup,
        policy=policy,
        session=object(),
        journal=journal,
        process_registry=registry,
        _flush_source_observations=lambda: None,
    )
    inspections = []

    def inspect():
        inspections.append(now[0])
        return identity

    native.verify_active = inspect
    return SimpleNamespace(
        native=native,
        now=now,
        journal=journal,
        original_expiry=now[0] + 10,
        inspections=inspections,
        initial=now[0],
    )


def test_identity_expiry_is_not_extended_by_a_fresh_continuity_observation(clock_native):
    g = clock_native
    current = NativeContinuity(g.native)
    g.now[0] = g.original_expiry - 1
    prepared, _ = current.prepare()
    assert prepared.observed.expires_at <= g.original_expiry
    current.acknowledge(prepared)
    assert current.expires_at <= g.original_expiry


def test_original_execution_parent_limit_remains_distinct_and_earlier(clock_native):
    g = clock_native
    # A separate original signed source may outlive the registered execution.
    ready = g.journal._append(
        "startup", g.journal._read(g.native.startup.ready_ref)["payload"], expires_at=g.initial + 90
    )
    g.native.startup.ready_ref = ready
    cap = min(g.native.process.deadline, g.native.process.invocation.relay.binding.deadline)
    current = NativeContinuity(g.native)
    g.now[0] = cap - 1
    prepared, _ = current.prepare()
    assert prepared.observed.expires_at <= cap
    current.acknowledge(prepared)
    assert current.expires_at <= cap


@pytest.mark.parametrize("phase", ["prepare", "acknowledge", "consume"])
@pytest.mark.parametrize("offset", [-0.001, 0, 0.001])
def test_identity_boundary_refuses_at_and_after_expiry(clock_native, phase, offset):
    g = clock_native
    current = NativeContinuity(g.native)
    prepared, _ = current.prepare()
    current.acknowledge(prepared)
    g.now[0] = g.original_expiry + offset

    def effect():
        if phase == "prepare":
            return current.prepare()
        if phase == "acknowledge":
            return current.acknowledge(prepared)
        return current.check(prepared)

    if offset < 0:
        effect()
        assert current.expires_at <= g.original_expiry
    else:
        before = len(g.inspections)
        with pytest.raises(AccessDenied):
            effect()
        assert len(g.inspections) == before


def test_oversized_acknowledgement_cannot_replace_the_identity_limit(clock_native):
    g = clock_native
    current = NativeContinuity(g.native)
    prepared, _ = current.prepare()
    before = current.expires_at
    oversized = replace(
        prepared, observed=replace(prepared.observed, expires_at=g.original_expiry + 1)
    )
    with pytest.raises(AccessDenied):
        current.acknowledge(oversized)
    assert current.expires_at == before


@pytest.mark.parametrize(
    "kind", ["identity_limit", "process_cap", "identity_source", "owner_cap", "observation_expiry"]
)
def test_original_cap_or_owner_drift_cannot_be_renewed(clock_native, kind):
    g = clock_native
    current = NativeContinuity(g.native)
    if kind == "identity_limit":
        current._limits = replace(current._limits, identity_not_after=g.initial + 80)
    elif kind == "process_cap":
        g.native.process.deadline += 10
    elif kind == "identity_source":
        current.original["expires_at"] += 10
    elif kind == "owner_cap":
        current.deadline += 10
    else:
        current.expires_at = current.deadline
    with pytest.raises(AccessDenied):
        current.prepare()


def test_expiry_during_preparation_cannot_issue_an_observation(clock_native):
    g = clock_native
    current = NativeContinuity(g.native)
    original = g.native.verify_active

    def cross_boundary():
        result = original()
        g.now[0] = g.original_expiry
        return result

    g.native.verify_active = cross_boundary
    count = len(g.journal._history("trace"))
    with pytest.raises(AccessDenied):
        current.prepare()
    assert len(g.journal._history("trace")) == count


def test_expired_identity_refuses_effect_while_actual_owned_native_is_alive(
    composition, journal, monkeypatch
):
    # The provider fixture and renewal graph must start from the same authored
    # clock, so neither dependency nor native identity expires first by setup drift.
    initial = journal.clock()
    monkeypatch.setattr(journal, "clock", lambda: initial)
    append = journal._append

    def shorter(kind, payload, *, sources=(), expires_at=None):
        if kind == "startup":
            # Allow the unchanged 20-second collection bound, monitor interval
            # and measured preparation cost before the first renewal.
            expires_at = min(expires_at, journal.clock() + 30)
        return append(kind, payload, sources=sources, expires_at=expires_at)

    monkeypatch.setattr(journal, "_append", shorter)
    # This existing helper drives renewal manually and keeps its finite 55-second
    # execution cap. It makes no background-monitor coverage claim.
    g = renewal_graph(composition, monkeypatch, journal)
    native, authority = g.native, g.owner
    current = authority.continuity
    assert authority._thread is None
    original = journal._read(native.startup.ready_ref)
    expiry = original["expires_at"]
    prepared, _ = current.prepare()
    current.acknowledge(prepared)
    assert g.clock[0] < expiry < current.execution_not_after
    assert authority._expires_at == expiry
    assert all(
        record.value.expires_at <= expiry for record in g.workers.authorities._prepared.values()
    )
    identity = native.process.verify_identity()
    marker = native.pair.worktree / "forbidden-after-identity-expiry"
    observed_before = [
        event
        for event in g.core.notifications.list_events(g.handle)
        if event.kind == "scheduling_authority_renewed"
    ]
    assert observed_before
    # Current identity and Core authority must both work immediately before
    # the same expiry boundary; another precondition cannot explain refusal.
    g.clock[0] = expiry - 0.001
    current.check(prepared)
    before = g.core.scheduling.explain(g.handle, g.task.id)
    assert before["state"] == "ready" and before["expires_at"] == expiry
    with g.core.execution.work_boundary(g.handle, g.attempt.id, g.attempt.version):
        authority.check_prepared()
    g.clock[0] = expiry
    assert native.process.verify_identity() == identity
    with pytest.raises(
        AccessDenied, match="Native continuity no longer has its original authority"
    ):
        current.check(prepared)
    with pytest.raises(AccessDenied, match="Expired, stopped or replaced native continuity"):
        current.prepare()
    with pytest.raises(AccessDenied, match="Expired work authority cannot be revived"):
        authority.refresh()
    assert [
        event
        for event in g.core.notifications.list_events(g.handle)
        if event.kind == "scheduling_authority_renewed"
    ] == observed_before
    after = g.core.scheduling.explain(g.handle, g.task.id)
    assert after["source_revision"] == before["source_revision"]
    assert after["expires_at"] == expiry
    assert [blocker["code"] for blocker in after["blockers"]] == ["dependency_snapshot_expired"]
    with pytest.raises(SchedulingBlocked, match="^Obtain a fresh trusted dependency snapshot$"):
        with g.core.execution.work_boundary(g.handle, g.attempt.id, g.attempt.version):
            marker.write_text("forbidden")
    assert not marker.exists()
    assert journal._read(native.startup.ready_ref, fresh=False) == original
    assert native.process.verify_identity() == identity
    assert g.workers.stop(
        g.handle, g.attempt.id, operation_key="identity-expired-stop"
    ).state == "paused"
    assert native.process._closed
    assert authority.close()
