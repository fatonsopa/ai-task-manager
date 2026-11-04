"""Owned authored processes test pre-Core waiting; no real model or account evidence."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest

from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity
from ai_task_manager.adapters.hosts.bridge.startup import HostStartupReceipts
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.test_identity_observations import identity


@pytest.mark.parametrize("caller", ["startup", "continuity", "admission"])
@pytest.mark.parametrize("change", [None, "expired", "model"])
def test_precore_consumers_wait_and_recheck(bootstrap_graph, monkeypatch, caller, change):
    item, _, identities = identity(bootstrap_graph)
    native = item.native
    guard = native.startup.model_guard
    ready, held, release = Event(), Event(), Event()

    def pause_after(function):
        def paused(*args, **kwargs):
            result = function(*args, **kwargs)
            if guard._lock._is_owned():
                return result
            ready.set()
            assert held.wait(3)
            return result

        return paused

    if caller == "startup":
        receipts = HostStartupReceipts(
            item.journal, native.process_registry, hosts={native.binding.attempt_id: native}
        )
        monkeypatch.setattr(
            native.startup, "assert_stable", pause_after(native.startup.assert_stable)
        )

        def consume():
            return receipts.verify_host_ready(native.startup.ready_ref)
    elif caller == "continuity":
        continuity = NativeContinuity(native)
        from ai_task_manager.adapters.hosts.bridge import continuity as continuity_module

        # Freeze the race after construction, before the local current check.
        monkeypatch.setattr(
            continuity_module, "PreparedStartup", pause_after(continuity_module.PreparedStartup)
        )
        consume = continuity.prepare
    else:
        monkeypatch.setattr(native, "verify_active", pause_after(native.verify_active))

        def consume():
            return item.observations._inspect(
                native, native.startup.ready_ref, identities=identities
            )

    def hold():
        assert ready.wait(3)
        with guard.observation_boundary(not_after=native.binding.deadline):
            held.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(hold)
        checking = pool.submit(consume)
        try:
            assert held.wait(3)
            with pytest.raises(TimeoutError):
                checking.result(timeout=0.1)
            if change == "expired":
                item.journal.clock = lambda: native.binding.deadline
            elif change == "model":
                guard.refusal = "authored model refusal"
        finally:
            release.set()
        holding.result(timeout=3)
        if change is None:
            assert checking.result(timeout=3)
        else:
            with pytest.raises(AccessDenied):
                checking.result(timeout=3)


@pytest.mark.parametrize("version", [1, 2])
def test_evaluation_consumption_refuses_contention_inside_core(bootstrap_graph, version):
    from tests.hosts.bootstrap_helpers import grant_graph
    from tests.hosts.test_identity_consumers import v2

    if version == 1:
        graph = grant_graph(bootstrap_graph())
        item, grant, core = graph.item, graph.grant, graph.core
    else:
        item, graph, _, _, grant, _, _ = v2(bootstrap_graph)
        core = graph.core
    prepared = grant.prepare_current()
    held, release = Event(), Event()

    def hold():
        with item.native.startup.model_guard.observation_boundary(not_after=item.binding.deadline):
            held.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holding = pool.submit(hold)
        assert held.wait(3)
        try:
            with core.context.db.transaction(), pytest.raises(AccessDenied, match="contended"):
                prepared.check()
        finally:
            release.set()
        holding.result(timeout=3)
    assert item.native.startup.model_guard.refusal is None
