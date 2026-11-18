"""Owned authored native/Core controls; no actual account or provider operations."""

from threading import Event, Thread
from types import SimpleNamespace

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor import authority as module
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_continuity import AuthoredDependencyCollection


def graph(composition, monkeypatch, journal):
    value = composition()
    runtime, task, _, workers, factory, _, _ = value
    core, handle = runtime.rig.core, runtime.rig.handle
    clock = [journal.clock()]
    core.context.clock = journal.clock = lambda: clock[0]
    # A separate finite fixture window makes an intermediate renewal necessary.
    workers.deadline = clock[0] + 55
    source = AuthoredDependencyCollection(core, handle)
    workers.bind_dependencies(source)
    monkeypatch.setattr(module.AttemptAuthority, "start", lambda self: self.ensure_current())
    attempt = start(value)
    native = factory.owned[attempt.id]
    owner = workers._freshness[attempt.id]
    return SimpleNamespace(
        runtime=runtime,
        core=core,
        handle=handle,
        clock=clock,
        source=source,
        owner=owner,
        native=native,
        workers=workers,
        attempt=attempt,
        task=task,
    )


def renewals(g):
    return [
        e
        for e in g.core.notifications.list_events(g.handle)
        if e.kind == "scheduling_authority_renewed"
    ]


def delay(g, monkeypatch, seconds, after=None):
    original = g.source.prepare
    reached = []

    def collect(*args, **kwargs):
        reached.append(g.clock[0])
        g.clock[0] += seconds
        value = original(*args, **kwargs)
        if after is not None:
            after()
        return value

    monkeypatch.setattr(g.source, "prepare", collect)
    return reached


def test_l01_slow_read_gets_fresh_continuity_after_collection(composition, monkeypatch, journal):
    g = graph(composition, monkeypatch, journal)
    old_expiry = g.owner._expires_at
    next_at = g.owner._next_refresh_at
    assert next_at <= old_expiry - 21
    reached = delay(g, monkeypatch, 10.368)
    g.clock[0] = next_at + 0.001
    g.owner.ensure_current()
    assert len(reached) == 1 and g.clock[0] < old_expiry
    assert g.owner._current.observed.expires_at > old_expiry
    assert g.owner._current.observed.expires_at <= g.native.binding.deadline
    assert g.owner._current.observed.expires_at > g.clock[0] + 20
    assert len(renewals(g)) == 2
    assert g.owner._failure is None


@pytest.mark.parametrize("seconds", [20.001, 31])
def test_l02_overrun_does_not_publish_or_revive(composition, monkeypatch, journal, seconds):
    g = graph(composition, monkeypatch, journal)
    before = len(renewals(g))
    reached = delay(g, monkeypatch, seconds)
    with pytest.raises(AccessDenied):
        g.owner.refresh()
    assert reached and len(renewals(g)) == before
    assert g.owner._failure == "AccessDenied"


@pytest.mark.parametrize("boundary", [0, 0.001])
def test_l03_prior_expiry_at_or_before_return_refuses(composition, monkeypatch, journal, boundary):
    g = graph(composition, monkeypatch, journal)
    g.clock[0] = g.owner._expires_at - 10
    before = len(renewals(g))
    reached = delay(g, monkeypatch, 10 + boundary)
    with pytest.raises(AccessDenied, match="cannot be revived"):
        g.owner.refresh()
    assert reached and len(renewals(g)) == before


def test_l04_identity_expiry_during_read_is_not_authentication(composition, monkeypatch, journal):
    g = graph(composition, monkeypatch, journal)
    g.owner.continuity.expires_at = g.clock[0] + 5
    before = len(renewals(g))
    reached = delay(g, monkeypatch, 6)
    with pytest.raises(AccessDenied, match="cannot be renewed"):
        g.owner.refresh()
    assert reached and len(renewals(g)) == before


@pytest.mark.parametrize("change", ["hold", "source", "close"])
def test_l05_changes_during_read_publish_nothing(composition, monkeypatch, journal, change):
    g = graph(composition, monkeypatch, journal)
    before = len(renewals(g))

    def changed():
        if change == "hold":
            g.runtime.hold(g.workers.dispatches[g.attempt.id])
        elif change == "source":
            g.owner.registry.dependencies = object()
        else:
            g.owner.request_close()

    reached = delay(g, monkeypatch, 10.368, changed)
    with pytest.raises(Exception):
        g.owner.refresh()
    assert reached and len(renewals(g)) == before
    if change == "close":
        assert g.owner.collection_close_observations
        assert g.owner._failure is None


def test_l06_cap_covered_skips_read_but_not_current_guard(composition, monkeypatch, journal):
    g = graph(composition, monkeypatch, journal)
    # A fresh bounded renewal can cover the remainder of the original window.
    g.clock[0] += 25
    g.owner.refresh()
    assert g.owner._expires_at == g.owner.continuity.execution_not_after
    before = g.source.calls
    g.clock[0] += 5
    g.owner.ensure_current()
    assert g.source.calls == before
    g.owner.continuity.close()
    with pytest.raises(AccessDenied):
        g.owner.ensure_current()


def test_l07_single_collector_and_close_keep_publication_fenced(composition, monkeypatch, journal):
    g = graph(composition, monkeypatch, journal)
    entered, release = Event(), Event()
    original = g.source.prepare
    errors = []

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(g.source, "prepare", blocked)
    before = len(renewals(g))

    def collect():
        try:
            g.owner.refresh()
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=collect)
    worker.start()
    try:
        assert entered.wait(2)
        g.owner.ensure_current()
        g.owner.request_close()
        release.set()
        worker.join(timeout=3)
        assert not worker.is_alive() and len(errors) == 1
        assert len(renewals(g)) == before
        assert g.owner.collection_close_observations
    finally:
        release.set()
        worker.join(timeout=3)


def test_l08_no_positive_window_refuses_without_spin(composition, monkeypatch, journal):
    g = graph(composition, monkeypatch, journal)
    prepare = g.owner.continuity.prepare

    def short():
        native, ref = prepare()
        from dataclasses import replace

        native = replace(native, observed=replace(native.observed, expires_at=g.clock[0] + 20))
        return native, ref

    monkeypatch.setattr(g.owner.continuity, "prepare", short)
    before = len(renewals(g))
    with pytest.raises(AccessDenied, match="no safe next"):
        g.owner.refresh()
    assert len(renewals(g)) == before


@pytest.mark.parametrize("clock_kind", ["monotonic_overrun", "core_regression"])
def test_l09_each_collection_clock_must_remain_within_bound(
    composition, monkeypatch, journal, clock_kind
):
    g = graph(composition, monkeypatch, journal)
    before = len(renewals(g))
    reached = []
    original = g.source.prepare

    def collect(*args, **kwargs):
        reached.append(True)
        if clock_kind == "core_regression":
            g.clock[0] -= 1
        return original(*args, **kwargs)

    monkeypatch.setattr(g.source, "prepare", collect)
    if clock_kind == "monotonic_overrun":
        ticks = iter((0.0, 0.0, 21.0))
        monkeypatch.setattr(module, "diagnostic_clock", lambda: next(ticks, 21.0))
    with pytest.raises(AccessDenied, match="acceptance window"):
        g.owner.refresh()
    assert reached and len(renewals(g)) == before


def test_l10_actual_monitor_refreshes_early_and_retains_genuine_failure(
    composition, monkeypatch, journal
):
    real_start = module.AttemptAuthority.start
    g = graph(composition, monkeypatch, journal)
    monkeypatch.setattr(module.AttemptAuthority, "start", real_start)
    old_expiry = g.owner._expires_at
    g.owner.start()
    g.source.fail = True
    g.clock[0] = g.owner._next_refresh_at + 0.001
    assert old_expiry - g.clock[0] >= 20
    assert g.source.failed.wait(3), "Actual monitor did not collect within its bounded cadence"
    g.owner._thread.join(timeout=4)
    assert not g.owner._thread.is_alive()
    assert g.owner._failure == "AccessDenied"
    assert g.native.process._closed
    assert g.core.get_attempt(g.handle, g.attempt.id).state == "unknown"
    with g.core.context.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM resource_leases WHERE attempt_id=?", (g.attempt.id,)
            ).fetchone()[0]
            == 2
        )
    assert g.owner.failure_diagnostics and not g.owner.collection_close_observations


def test_l11_slow_publication_is_measured_and_cannot_enter_refresh_loop(
    composition, monkeypatch, journal
):
    g = graph(composition, monkeypatch, journal)
    before = len(renewals(g))
    original = g.core.scheduling.renew
    reached = []

    def publish(*args, **kwargs):
        value = original(*args, **kwargs)
        reached.append(value)
        g.clock[0] += 10
        return value

    monkeypatch.setattr(g.core.scheduling, "renew", publish)
    with pytest.raises(AccessDenied, match="no safe next"):
        g.owner.refresh()
    assert reached and len(renewals(g)) == before + 1
    assert g.owner._failure == "AccessDenied"
    assert g.owner._preparation_latency >= 10
    # The completed Core event remains; the failed scheduler grants no effects.
    with pytest.raises(AccessDenied):
        g.owner.ensure_current()


def test_l12_monitor_contention_waits_outside_core_without_fabricated_failure(
    composition, monkeypatch, journal
):
    real_start = module.AttemptAuthority.start
    g = graph(composition, monkeypatch, journal)
    monkeypatch.setattr(module.AttemptAuthority, "start", real_start)
    g.owner.start()
    guard = g.native.startup.model_guard
    prepared = guard.prepare_effect()
    entered, release = Event(), Event()

    def ordinary_effect():
        with guard.current_boundary(prepared):
            entered.set()
            assert release.wait(4)

    effect = Thread(target=ordinary_effect)
    effect.start()
    try:
        assert entered.wait(2)
        # The monitor reaches its1s cadence while the other owned effect holds
        # the guard. It must not convert that contention into a model fence.
        assert not g.owner._stop.wait(1.25)
        assert g.owner._thread.is_alive() and g.owner._failure is None
        # This independent Core read proves the waiting monitor holds no Core lock.
        assert g.core.get_attempt(g.handle, g.attempt.id).state == "running"
        release.set()
        effect.join(timeout=2)
        assert not effect.is_alive()
        assert not g.owner._stop.wait(0.1)
        assert g.owner._failure is None and not g.owner.failure_diagnostics
    finally:
        release.set()
        effect.join(timeout=2)
        g.owner.close()


def test_l13_cached_monitor_owner_still_refuses_current_model_fence(
    composition, monkeypatch, journal
):
    real_start = module.AttemptAuthority.start
    g = graph(composition, monkeypatch, journal)
    monkeypatch.setattr(module.AttemptAuthority, "start", real_start)
    g.owner.start()
    with pytest.raises(AccessDenied):
        g.native.startup.model_guard.configuration_changed(None, record=False)
    g.owner._thread.join(timeout=4)
    assert not g.owner._thread.is_alive()
    assert g.owner._failure == "AccessDenied"
    assert g.native.process._closed
    assert g.core.get_attempt(g.handle, g.attempt.id).state == "unknown"
    with pytest.raises(AccessDenied):
        g.owner.check_prepared()


@pytest.mark.parametrize("expired", [False, True])
def test_l14_auth_collection_overlap_preserves_prior_expiry_fence(bootstrap_graph, expired):
    from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity
    from tests.hosts.test_identity_observations import identity

    item, permits, observer = identity(bootstrap_graph)
    before = observer.prepare().observation
    original = item.journal._read(item.native.startup.ready_ref)
    clock = [item.journal.clock()]
    item.journal.clock = lambda: clock[0]
    continuity = NativeContinuity(item.native, identities=observer)
    entered, release = Event(), Event()
    results, errors = [], []

    def collected():
        entered.set()
        assert release.wait(4)

    permits.after = collected

    def authenticate():
        try:
            results.append(observer.refresh(item.root / "overlap-auth"))
        except BaseException as error:
            errors.append(error)

    auth = Thread(target=authenticate)
    auth.start()
    try:
        assert entered.wait(3)
        # This represents the dependency collection's elapsed time, while an
        # actual owned authored status process has completed but is not issued.
        clock[0] = before.expires_at if expired else clock[0] + 10.368
        if expired:
            with pytest.raises(AccessDenied):
                continuity.prepare()
        else:
            prepared, _ = continuity.prepare()
            continuity.acknowledge(prepared)
            assert prepared.identity.observation is before
            assert prepared.observed.expires_at <= before.expires_at
        release.set()
        auth.join(timeout=3)
        assert not auth.is_alive()
        if expired:
            assert len(errors) == 1 and not results
            assert observer._current is before
            with pytest.raises(AccessDenied):
                continuity.prepare()
        else:
            assert not errors and len(results) == 1
            prepared, _ = continuity.prepare()
            assert prepared.identity.observation is observer._current
            assert prepared.observed.expires_at <= observer.execution_not_after
            assert item.journal._read(item.native.startup.ready_ref) == original
    finally:
        release.set()
        auth.join(timeout=3)
        continuity.close()


def test_l15_local_milestones_prepare_outside_running_model_guard(
    composition, monkeypatch, journal
):
    real_start = module.AttemptAuthority.start
    g = graph(composition, monkeypatch, journal)
    monkeypatch.setattr(module.AttemptAuthority, "start", real_start)
    prepared = Event()
    references = []
    guard = g.native.startup.model_guard

    def prepare_local(reference, *, task_id):
        assert task_id == g.task.id
        assert not guard._lock._is_owned()
        with g.core.context.db.transaction() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        references.append(reference)
        prepared.set()

    g.source.prepare_current_milestones = prepare_local
    calls = g.source.calls
    original_expiry = g.owner._expires_at
    try:
        g.owner.start()
        prepared.clear()
        assert prepared.wait(3), "Actual running observer skipped local milestone preparation"
        assert g.owner._failure is None
        assert g.source.calls == calls
        assert g.owner._expires_at == original_expiry
        assert references and set(references) == {g.owner._dependency_ref}
    finally:
        g.workers.stop(g.handle, g.attempt.id, operation_key="local-milestone-observer-stop")
