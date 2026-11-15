"""Concurrent owned stop over an authored child, real helper and Core settlement."""

import threading
from types import SimpleNamespace

import pytest

from ai_task_manager.supervisor.managed import ManagedDispatch
from tests.hosts.test_identity_stop_reconciliation import owned
from tests.hosts.test_managed_terminal import running_managed as running_managed


@pytest.mark.parametrize("outcome", ["complete", "incomplete", "changed_process", "changed_helper"])
def test_concurrent_owned_stop_settles_only_complete_unchanged_proof(running_managed, outcome):
    r, attempt, workers, native = running_managed
    dispatch = workers.dispatches[attempt.id]
    host, helper, records, _ = owned()
    host.process = native.process
    original_process = host.process
    physical_stops = []
    stop_process = original_process.stop

    def physical_stop():
        physical_stops.append(original_process.identity)
        return stop_process()

    original_process.stop = physical_stop
    workers.deadline = r.journal.clock() + (0.04 if outcome == "incomplete" else 2)
    host.binding = SimpleNamespace(attempt_id=attempt.id, deadline=workers.deadline)
    host.journal.clock = r.journal.clock
    host.failure_diagnostics, host.diagnostics_truncated = [], False
    entered, release = threading.Event(), threading.Event()

    def close_session():
        entered.set()
        assert release.wait(3), "Authored stop owner was not released"

    host.session.close = close_session
    record = host.permits.record_host_stop

    def recorded(binding, stopped):
        record(binding, stopped)
        # Change the owner while the first stop still holds its lock. The
        # waiting consumer must reject even a physically stopped original child.
        if outcome == "changed_process":
            host.process = SimpleNamespace(_closed=True, identity=object())
        elif outcome == "changed_helper":
            host.identity = object()

    host.permits.record_host_stop = recorded
    helper._thread = threading.Thread(target=lambda: helper._stop.wait(3))
    helper._thread.start()
    workers.dispatches[attempt.id] = ManagedDispatch(
        attempt.id, dispatch.resources, dispatch.waiting, host
    )
    result, errors = [], []

    def owner_stop():
        try:
            result.append(host.stop())
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=owner_stop)
    timer = None
    try:
        owner.start()
        assert entered.wait(1), "Concurrent owner never acquired its stop lock"
        r.hold(r.dispatches[0])
        if outcome != "incomplete":
            timer = threading.Timer(0.04, release.set)
            timer.start()
        stopped = workers.stop(r.rig.handle, attempt.id, operation_key="human-hold-stop")
        with r.rig.db.transaction() as db:
            leases = db.execute(
                "SELECT count(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)
            ).fetchone()[0]
            active = db.execute("SELECT active FROM attempts WHERE id=?", (attempt.id,)).fetchone()[
                0
            ]
        fact = r.journal.observations(attempt.id, "runtime_outcome")[-1]
        assert fact["model_success"] == "not_established"
        if outcome == "complete":
            assert stopped.state == "paused" and not active
            assert fact["quiescent"] is True and fact["stop_components"]["native"] is True
            assert leases == 0
            assert not helper._thread.is_alive() and original_process._closed
        else:
            assert stopped.state == "unknown" and active
            assert fact["quiescent"] is False and fact["stop_components"]["native"] is False
            assert leases == 2
            assert dispatch.resources.worktree.exists() and dispatch.resources.database.exists()
            if outcome == "incomplete":
                assert owner.is_alive() and not original_process._closed
    finally:
        release.set()
        if timer is not None:
            timer.join(1)
        owner.join(2)
        helper.request_stop()
        helper._thread.join(1)
    assert not errors and not owner.is_alive()
    assert result[0].confirmed and len(physical_stops) == 1
