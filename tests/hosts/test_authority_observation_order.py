"""Pre-Core authority contention with owned fixtures; no live model/provider calls."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.authority import AttemptAuthority
from tests.hosts.test_authority_shutdown import graph
from tests.hosts.test_composition import composition as composition


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("change", [None, "close", "deadline", "expiry", "model", "source"])
def test_gateway_authority_waits_before_core_and_rechecks(composition, monkeypatch, change, busy):
    monkeypatch.setattr(AttemptAuthority, "start", lambda self: self.ensure_current())
    g = graph(composition)
    guard = g.native.startup.model_guard
    held, release, waiting = Event(), Event(), Event()
    original = guard.observation_boundary
    dependencies = g.authority.registry.dependencies
    collecting, collected = Event(), Event()
    prepare = g.source.prepare

    def collect(*args, **kwargs):
        collecting.set()
        assert collected.wait(5)
        return prepare(*args, **kwargs)

    @contextmanager
    def observed(**kwargs):
        waiting.set()
        with original(**kwargs):
            yield

    monkeypatch.setattr(guard, "observation_boundary", observed)

    def hold():
        with guard._lock:
            held.set()
            assert release.wait(5)
            if change == "model":
                guard.refusal = "authored_model_refusal"

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            collector = None
            if busy:
                monkeypatch.setattr(g.source, "prepare", collect)
                g.clock[0] = g.authority._next_refresh_at + 0.001
                collector = pool.submit(g.authority.refresh)
                assert collecting.wait(2)
            holder = pool.submit(hold)
            assert held.wait(2)
            checked = pool.submit(g.native.gateway.delegate.check)
            try:
                if not waiting.wait(2):
                    checked.result(timeout=1)
                    pytest.fail("Gateway skipped the pre-Core observation boundary")
                assert not checked.done()
                # The waiting gateway must leave independent Core writers responsive.
                with g.runtime.rig.db.transaction() as connection:
                    assert connection.execute("SELECT 1").fetchone()[0] == 1
                if change == "close":
                    g.authority.request_close()
                elif change == "deadline":
                    g.clock[0] = g.authority.deadline
                elif change == "expiry":
                    g.clock[0] = g.authority._expires_at
                elif change == "source":
                    g.authority.registry.dependencies = object()
                if change in {"close", "deadline", "expiry"}:
                    # Cancellation/expiry must end the wait while the guard is held.
                    with pytest.raises(AccessDenied, match="stopped or expired"):
                        checked.result(timeout=1)
                else:
                    release.set()
                    if change == "model":
                        with pytest.raises(AccessDenied, match="refusal fence"):
                            checked.result(timeout=2)
                    elif change == "source":
                        with pytest.raises(AccessDenied, match="source or store changed"):
                            checked.result(timeout=2)
                    else:
                        checked.result(timeout=2)
                        assert guard.refusal is None
            finally:
                release.set()
                holder.result(timeout=2)
                collected.set()
                if collector is not None:
                    try:
                        collector.result(timeout=2)
                    except AccessDenied:
                        assert change is not None
    finally:
        collected.set()
        g.authority.registry.dependencies = dependencies
        g.workers.stop(g.handle, g.attempt.id, operation_key="observation-order-stop")


def test_authority_collection_precedes_model_observation_lock(composition, monkeypatch):
    monkeypatch.setattr(AttemptAuthority, "start", lambda self: self.ensure_current())
    g = graph(composition)
    guard = g.native.startup.model_guard
    prepare = g.source.prepare
    observed = []

    def acquire():
        acquired = guard._lock.acquire(blocking=False)
        if acquired:
            guard._lock.release()
        return acquired

    def collect(*args, **kwargs):
        # Another thread must be able to observe the model during provider I/O.
        with ThreadPoolExecutor(max_workers=1) as pool:
            observed.append(pool.submit(acquire).result(timeout=1))
        return prepare(*args, **kwargs)

    monkeypatch.setattr(g.source, "prepare", collect)
    try:
        g.clock[0] = g.authority._next_refresh_at + 0.001
        g.authority.ensure_current()
        assert observed == [True]
    finally:
        g.workers.stop(g.handle, g.attempt.id, operation_key="collection-order-stop")
