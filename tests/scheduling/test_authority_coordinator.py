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
def graph(tmp_path, request):
    p = ProviderFixture(tmp_path / "provider")
    r = p.rig
    task = p.task()
    milestone = getattr(request, "param", None)
    if milestone in {"global-milestone", "subject"}:
        from ai_task_manager.adapters.linear.dependencies.source import DependencySource
        from ai_task_manager.domains.scheduling.resources import AdmissionResources

        dependent = p.task(title="Other milestone subject") if milestone != "subject" else task
        predecessor = (
            task if milestone != "subject" else p.task(code=True, title="Code predecessor")
        )
        p.source = DependencySource(
            r.core, r.handle, p.client, p.receipts,
            milestones=((dependent.prose_digest, predecessor.prose_digest),),
        )
        for item in (task, predecessor):
            r.resource_sources.rows[item.id] = AdmissionResources(
                p.scope, item.id, item.prose_digest, item.decision_revision,
                "authored-project", (), (),
            )
        if milestone == "subject":
            p.source.refresh(predecessor.id, "predecessor")
            predecessor = r.transition(predecessor, "Todo", "enqueue")
            p.sync(predecessor)
            predecessor, previous = r.running(predecessor)
            predecessor = r.complete(predecessor)
            predecessor, _ = r.settle(predecessor, previous)
            merge = r.proof(
                "merge", predecessor.id,
                {"commit": "a" * 40, "prose_digest": predecessor.prose_digest},
                expires_at=r.now + 30,
            )
            predecessor = r.transition(predecessor, "In Dev", "merge", {"merge": merge})
            p.sync(predecessor)
            p.edge(predecessor, task)
    task = r.transition(task, "Todo", "enqueue")
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


@pytest.mark.parametrize("graph", ["global-milestone"], indirect=True)
def test_r03_non_subject_does_not_consume_superseded_milestone_receipt(graph):
    g = graph
    g.authority.refresh()
    original = g.authority._dependency_ref
    newer = g.p.source.prepare(g.task.id, "independent-inflight-collection")
    assert original != newer.snapshot_ref
    with pytest.raises(AccessDenied, match="superseded"):
        g.p.receipts.verify_snapshot(original)
    # The actual running coordinator must not treat another task's declared
    # merge milestone as a new receipt-consumption obligation for this task.
    g.authority.ensure_current()
    assert g.authority._failure is None and not g.native.stopped.is_set()


@pytest.mark.parametrize("graph", ["subject"], indirect=True)
@pytest.mark.parametrize("change", [None, "held", "source", "expired", "superseded", "tampered"])
def test_r03_milestone_uses_current_published_receipt(graph, change):
    from dataclasses import replace

    from ai_task_manager.storage import work

    g = graph
    g.authority.refresh()
    cached = g.authority._dependency_ref
    g.p.source.refresh(g.task.id, "independent-published-observation")
    with pytest.raises(AccessDenied, match="superseded"):
        g.p.receipts.verify_snapshot(cached)
    view = g.r.core.scheduling.explain(g.r.handle, g.task.id)
    calls = len(g.p.calls)
    if change == "held":
        task = g.r.core.get_task(g.r.handle, g.task.id)
        with g.r.db.transaction() as conn:
            work.save(conn, replace(task, human_hold=True), task.version)
    elif change == "source":
        g.p.source.source_id = "changed-source"
    elif change == "expired":
        g.r.now = view["expires_at"]
    elif change == "superseded":
        g.p.source.prepare(g.task.id, "unpublished-newer-observation")
        calls = len(g.p.calls)
    elif change == "tampered":
        with g.p.receipts.transaction() as conn:
            conn.execute("UPDATE records SET mac='invalid'")
    if change is not None:
        with pytest.raises(AccessDenied):
            g.authority._prepare_milestones()
    else:
        g.authority._prepare_milestones()
        assert g.authority._failure is None
        assert g.r.core.scheduling.explain(g.r.handle, g.task.id) == view
    assert len(g.p.calls) == calls


@pytest.mark.parametrize("graph", ["subject"], indirect=True)
@pytest.mark.parametrize("change", [None, "held", "source", "cancelled", "expired"])
@pytest.mark.parametrize("separate_source", [False, True])
def test_r03_milestone_waits_for_current_producer_publication(
    graph, monkeypatch, change, separate_source
):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from ai_task_manager.storage import work

    g = graph
    g.authority.refresh()
    cached = g.authority._dependency_ref
    entered, release, waiting = Event(), Event(), Event()
    boundary = g.p.source.publication_boundary

    def collecting(query, variables):
        if "DependencySchema" in query:
            entered.set()
            assert release.wait(3)

    @contextmanager
    def observed(task_id, **kwargs):
        if kwargs.get("cancelled") is g.authority._stop:
            waiting.set()
        with boundary(task_id, **kwargs):
            yield

    g.p.callback = collecting
    monkeypatch.setattr(g.p.source, "publication_boundary", observed)
    source = g.p.source
    if separate_source:
        from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
        from ai_task_manager.adapters.linear.dependencies.source import DependencySource

        receipts = DependencyReceipts.open_existing(
            g.p.receipts.path, g.p.receipts.key_path, clock=lambda: g.r.now
        )
        source = DependencySource(
            g.r.core, g.r.handle, g.p.client, receipts, milestones=g.p.source.milestones
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(source.refresh, g.task.id, "concurrent-observation")
        assert entered.wait(2)
        consumer = pool.submit(g.authority._prepare_milestones)
        try:
            assert waiting.wait(2) and not consumer.done()
            with g.r.db.transaction() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            if change == "held":
                task = g.r.core.get_task(g.r.handle, g.task.id)
                with g.r.db.transaction() as conn:
                    work.save(conn, replace(task, human_hold=True), task.version)
            elif change == "source":
                g.p.source.milestones = ()
            elif change == "cancelled":
                g.authority.request_close()
            elif change == "expired":
                g.r.now = g.authority._expires_at
            release.set()
            if change is None:
                producer.result(timeout=3)
                consumer.result(timeout=3)
                assert g.authority._failure is None and not g.native.stopped.is_set()
                with pytest.raises(AccessDenied, match="superseded"):
                    g.p.receipts.verify_snapshot(cached)
            else:
                with pytest.raises((WorkError, AssertionError)):
                    consumer.result(timeout=3)
                try:
                    producer.result(timeout=3)
                except (WorkError, AssertionError):
                    pass
        finally:
            release.set()


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
