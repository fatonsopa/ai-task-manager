"""Authored thread/lock controls; no actual account, provider or model effects."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.test_model_stability import claude_control


@contextmanager
def nothing(**kwargs):
    yield


def test_transport_current_observation_waits_before_core(native, binding, journal):
    _, _, _, observer, initialize = claude_control(native, binding, journal)
    initialize()
    guard = observer.model_guard
    prepared, held, release = Event(), Event(), Event()
    calls = []
    host = object.__new__(ManagedNativeHost)
    host.journal, host.binding = journal, binding
    host.policy = SimpleNamespace(check=lambda: None)
    host._source_audit_boundary = nothing
    host._flush_source_observations = lambda: None

    def stable():
        observer.assert_stable()
        prepared.set()
        assert held.wait(2)

    def check():
        with guard.startup_current_boundary():
            calls.append("authority")

    host.startup = SimpleNamespace(assert_stable=stable, model_guard=guard)
    host.authority = SimpleNamespace(check_prepared=check)
    host.core = SimpleNamespace(
        execution=SimpleNamespace(validate_work_lease=lambda *args: calls.append("core"))
    )

    def hold():
        assert prepared.wait(2)
        with guard._lock:
            held.set()
            assert release.wait(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(hold)
        current = pool.submit(host._current_lease, object(), SimpleNamespace(id="a", version=1))
        assert held.wait(2)
        try:
            # A valid independent observation must not turn transient contention
            # before Core acquisition into a failed native transport.
            assert not current.done() or current.exception() is None
        finally:
            release.set()
        holding.result(timeout=2)
        current.result(timeout=2)
    assert calls == ["authority", "core"]


def test_identity_preparation_wait_is_cancelled_on_close(native, binding, journal, tmp_path):
    _, _, _, startup, initialize = claude_control(native, binding, journal)
    initialize()
    guard = startup.model_guard
    observer = object.__new__(NativeIdentityObservations)
    observer.native = SimpleNamespace(startup=startup)
    observer.execution_not_after = journal.clock() + 30
    observer.journal = journal
    # No identity can be produced here: close must interrupt the guard wait
    # before the rest of preparation or any collection runs.
    observer._bound_current = lambda: pytest.fail("cancelled preparation was admitted")
    helper = IdentityRefresh(observer, tmp_path / "identity", 1)
    held, release, entered = Event(), Event(), Event()

    def holding():
        with guard._lock:
            held.set()
            assert release.wait(3)

    def preparing():
        entered.set()
        with pytest.raises(AccessDenied):
            helper.ensure_current()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(holding)
        assert held.wait(2)
        waiting = pool.submit(preparing)
        assert entered.wait(2)
        helper._stop.set()
        try:
            waiting.result(timeout=1)
        finally:
            release.set()
        holder.result(timeout=2)
    assert helper.close()


def test_stop_requests_identity_before_transport_and_joins_after():
    host = object.__new__(ManagedNativeHost)
    events = []
    helper = SimpleNamespace(
        request_stop=lambda: events.append("request"),
        close=lambda: events.append("join") or True,
    )
    host.identity = host._identity_bound = helper
    host._source_audit_boundary = nothing
    host._session_close_attempted = False
    host.session = SimpleNamespace(close=lambda: events.append("session"))
    host.portal = host._portal_context = None
    from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop

    result = GroupStop(True, GroupObservation(True))
    host.process = SimpleNamespace(
        begin_shutdown=lambda: events.append("cancel_reads"),
        stop=lambda: events.append("process") or result,
    )
    host.permits = SimpleNamespace(record_host_stop=lambda *a: events.append("record"))
    host.binding = SimpleNamespace(attempt_id="authored")
    host.journal = SimpleNamespace(_append=lambda *a: pytest.fail("unexpected failure record"))
    assert host._stop_owned() is result
    assert events == ["cancel_reads", "request", "session", "process", "join", "record"]


def test_required_identity_window_wait_is_cancelled_before_another_auth(
    bootstrap_graph, monkeypatch
):
    from tests.hosts.test_identity_observations import identity

    item, _, observer = identity(bootstrap_graph)
    helper = IdentityRefresh(observer, item.root / "required-window", 1)
    expiry = observer.prepare().observation.expires_at
    item.journal.clock = lambda: expiry - 18
    prepared = Event()
    original = observer.prepare

    def observed(**kwargs):
        result = original(**kwargs)
        prepared.set()
        return result

    monkeypatch.setattr(observer, "prepare", observed)
    before = item.accounting.number
    helper._collect.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(helper.ensure_current, minimum_remaining_seconds=21)
            assert prepared.wait(2)
            helper.request_stop()
            with pytest.raises(AccessDenied):
                waiting.result(timeout=1)
        assert item.accounting.number == before
    finally:
        helper._collect.release()
        assert helper.close()


def test_real_identity_thread_close_cancels_model_wait(bootstrap_graph, monkeypatch):
    from threading import current_thread

    from tests.hosts.test_identity_observations import identity

    item, _, observations = identity(bootstrap_graph)
    helper = IdentityRefresh(observations, item.root / "close-loop", 1)
    guard = item.native.startup.model_guard
    entering = Event()
    original = guard.observation_boundary

    @contextmanager
    def observed(**kwargs):
        if current_thread().name == "managed-identity-refresh":
            entering.set()
        with original(**kwargs):
            yield

    monkeypatch.setattr(guard, "observation_boundary", observed)
    helper.start()
    before = item.accounting.number
    try:
        with guard._lock:
            assert entering.wait(3)
            assert helper.close()
            assert not helper._thread.is_alive()
        assert item.accounting.number == before
        with pytest.raises(AccessDenied):
            observations.prepare()
    finally:
        assert helper.close()


@pytest.mark.parametrize("change", ["model", "expiry"])
def test_waiting_observation_rechecks_current_refusal(native, binding, journal, change):
    _, _, _, observer, initialize = claude_control(native, binding, journal)
    initialize()
    guard = observer.model_guard
    entered = Event()
    deadline = journal.clock() + 20

    def waiting():
        entered.set()
        with guard.observation_boundary(not_after=deadline):
            pytest.fail("changed authority admitted")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with guard._lock:
            pending = pool.submit(waiting)
            assert entered.wait(2)
            if change == "model":
                guard.refusal = "authored changed model"
            else:
                journal.clock = lambda: deadline
        with pytest.raises(AccessDenied):
            pending.result(timeout=2)


def test_unstopped_identity_still_downgrades_confirmed_native_stop():
    from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop

    host = object.__new__(ManagedNativeHost)
    helper = SimpleNamespace(request_stop=lambda: None, close=lambda: False)
    host.identity = host._identity_bound = helper
    host._source_audit_boundary = nothing
    host._session_close_attempted = True
    host.session = host.portal = host._portal_context = None
    host.process = SimpleNamespace(
        begin_shutdown=lambda: None, stop=lambda: GroupStop(True, GroupObservation(True))
    )
    records = []
    host.permits = SimpleNamespace(record_host_stop=lambda binding, stop: records.append(stop))
    host.binding = SimpleNamespace(attempt_id="authored")
    host.journal = SimpleNamespace(_append=lambda *args: None)
    result = host._stop_owned()
    assert not result.confirmed and result.reason == "identity_helper_stop_unknown"
    assert records == [result]
