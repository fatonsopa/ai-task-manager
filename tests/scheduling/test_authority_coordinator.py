"""Production dependency reader plus authored native continuity, no external calls."""

from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.domains.work.errors import AccessDenied, WorkError
from ai_task_manager.supervisor.authority import WorkAuthorities
from tests.scheduling.provider_fixture import ProviderFixture


class AuthoredObservationGuard:
    """Mechanical observation boundaries only, never model authentication evidence."""

    def __init__(self, native, clock):
        self.native, self.clock = native, clock
        self.deadline = native.binding.deadline

    @contextmanager
    def observation_boundary(self, *, cancelled=None, not_after=None):
        if (
            self.native.stopped.is_set()
            or (cancelled is not None and cancelled.is_set())
            or self.clock() >= min(self.deadline, not_after or self.deadline)
        ):
            raise AccessDenied("Authored observation stopped or expired")
        yield

    def prepare_effect(self):
        # No model receipt is produced by this mechanical fixture.
        with self.observation_boundary():
            return None

    @contextmanager
    def current_boundary(self, prepared):
        if prepared is not None:
            raise AccessDenied("Authored observation has no model receipt")
        with self.observation_boundary():
            yield


class AuthoredContinuity:
    """Only a mechanical observation port, never native startup/auth proof."""

    def __init__(self, native, clock):
        self.native, self.clock = native, clock
        self._execution_not_after = native.binding.deadline
        self.expires_at = clock() + 30
        self.closed = False

    @property
    def execution_not_after(self):
        return self._execution_not_after

    def prepare(self):
        if self.closed or self.clock() >= self.expires_at:
            raise AccessDenied("Authored continuity expired or unknown")
        return SimpleNamespace(
            observed=SimpleNamespace(expires_at=self.clock() + 30)
        ), "authored-current"

    def check(self, value):
        if self.closed or self.clock() >= min(self.expires_at, value.observed.expires_at):
            raise AccessDenied("Authored continuity expired or stopped")
        return value

    def acknowledge(self, value):
        self.check(value)
        self.expires_at = value.observed.expires_at

    def close(self):
        self.closed = True


@pytest.fixture
def graph(tmp_path):
    p = ProviderFixture(tmp_path / "provider")
    r = p.rig
    task = r.transition(p.task(), "Todo", "enqueue")
    p.sync(task)
    p.source.refresh(task.id, "initial-collection")
    task, attempt = r.running(task)
    p.sync(task)
    r.now += 1
    sid, scope = r.core.session_binding(r.handle)
    native = SimpleNamespace(
        identity=None,  # Mechanical continuity only; no native identity refresh authority.
        binding=SimpleNamespace(
            scope=scope,
            core_session=sid,
            attempt_id=attempt.id,
            worker_id=attempt.worker_id,
            host=attempt.host,
            deadline=r.now + 100,
            configuration_digest="a" * 64,
        ),
        journal=HostJournal(tmp_path / "host/journal", tmp_path / "host/key", clock=lambda: r.now),
    )
    native.stopped = Event()
    native.stop = native.stopped.set
    native.startup = SimpleNamespace(model_guard=AuthoredObservationGuard(native, lambda: r.now))
    continuity = AuthoredContinuity(native, lambda: r.now)
    registry = WorkAuthorities(r.core, p.source)
    r.core.scheduling.renewals = registry
    authority = registry.attach(r.handle, attempt, native, continuity)
    yield SimpleNamespace(
        p=p,
        r=r,
        task=task,
        attempt=attempt,
        native=native,
        registry=registry,
        authority=authority,
        continuity=continuity,
    )
    assert authority.close()


def test_production_collection_sustains_multiple_windows_without_extending_execution(graph):
    g = graph
    deadline = g.native.binding.deadline
    for _ in range(4):
        assert g.authority.refresh()["state"] == "ready"
        assert g.r.core.execution.validate_work_lease(g.r.handle, g.attempt.id, g.attempt.version)
        g.r.now += 20
    assert g.p.calls and g.native.binding.deadline == deadline == g.authority.deadline
    assert g.authority.close()
    with pytest.raises(AccessDenied):
        g.authority.refresh()


@pytest.mark.parametrize(
    "kind",
    [
        "provider_failure",
        "rebound_source",
        "stop_during_collection",
        "unknown_native",
        "execution_expired",
        "replaced_worker",
    ],
)
def test_failed_refresh_is_sticky_and_has_no_later_work_permission(graph, kind):
    g = graph
    if kind == "provider_failure":
        g.p.disabled = True
    elif kind == "rebound_source":
        g.registry.dependencies = object()
    elif kind == "stop_during_collection":
        g.p.callback = lambda *args: g.authority.close()
    elif kind == "unknown_native":
        g.continuity.closed = True
    elif kind == "execution_expired":
        g.r.now = g.authority.deadline
    else:
        g.native.binding.worker_id = "foreign-worker"
    with pytest.raises((WorkError, AssertionError)):
        g.authority.refresh()
    assert not any(
        e.kind == "scheduling_authority_renewed"
        for e in g.r.core.notifications.list_events(g.r.handle)
    )
    with pytest.raises(AccessDenied):
        g.authority.ensure_current()


def test_consumption_uses_prepared_metadata_and_never_calls_provider(graph):
    g = graph
    g.authority.refresh()
    g.p.disabled = True
    with g.r.core.execution.work_boundary(g.r.handle, g.attempt.id, g.attempt.version):
        g.authority.check_prepared()
    assert g.p.depth == 0


def test_refresh_thread_fences_and_stops_after_source_failure(graph):
    g = graph
    g.authority.refresh()
    g.p.disabled = True

    # Deterministic wake: no long wall-clock sleeps and no fabricated clock proof.
    def wake(seconds):
        g.r.now += 21
        return False

    g.authority._stop.wait = wake
    g.authority.start()
    assert g.native.stopped.wait(2)
    assert g.authority._thread is not None
    g.authority._thread.join(timeout=2)
    assert not g.authority._thread.is_alive()
    with pytest.raises(AccessDenied):
        g.authority.check_prepared()


def test_execution_cap_drift_refuses_before_provider_collection(graph):
    g = graph
    before = len(g.p.calls)
    g.authority.deadline += 10
    with pytest.raises(AccessDenied):
        g.authority.refresh()
    assert len(g.p.calls) == before


def test_replaced_core_store_refuses_before_provider_collection(graph, tmp_path):
    from ai_task_manager.storage.database import Database

    g = graph
    original = g.r.core.context.db
    before = len(g.p.calls)
    g.r.core.context.db = Database(tmp_path / "foreign.sqlite")
    try:
        with pytest.raises(AccessDenied):
            g.authority.refresh()
        assert len(g.p.calls) == before
    finally:
        g.r.core.context.db = original


def test_refresh_supervisor_spans_virtual_work_windows_without_model_authority(graph):
    g = graph
    g.authority.refresh()
    windows = []

    def wake(seconds):
        assert 0 < seconds <= 5
        if len(windows) == 3:
            return True
        g.r.now += 20
        windows.append(g.r.now)
        return False

    g.authority._stop.wait = wake
    g.authority.start()
    g.authority._thread.join(timeout=3)
    assert not g.authority._thread.is_alive()
    assert windows == [1021, 1041, 1061]
    assert g.r.core.execution.validate_work_lease(g.r.handle, g.attempt.id, g.attempt.version)
    assert g.authority.deadline == 1101
    assert not g.native.stopped.is_set()


def test_refresh_supervisor_stops_for_human_hold_before_next_collection(graph):
    from tests.scheduling.test_renewal import hold

    g = graph
    g.authority.refresh()
    before = len(g.p.calls)
    hold(g.r, g.task)
    g.authority._stop.wait = lambda seconds: False
    g.authority.start()
    assert g.native.stopped.wait(2)
    g.authority._thread.join(timeout=2)
    assert not g.authority._thread.is_alive()
    assert len(g.p.calls) == before
