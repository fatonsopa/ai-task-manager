"""Real owned authored processes and Core; no actual host/account/provider calls."""

from threading import Event, Thread
from types import SimpleNamespace

import pytest

from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_continuity import AuthoredDependencyCollection


def graph(composition):
    value = composition()
    runtime, task, _, workers, factory, _, _ = value
    core, handle = runtime.rig.core, runtime.rig.handle
    clock = [core.context.clock()]
    core.context.clock = lambda: clock[0]
    source = AuthoredDependencyCollection(core, handle)
    workers.bind_dependencies(source)
    attempt = start(value)
    return SimpleNamespace(
        value=value,
        runtime=runtime,
        task=task,
        workers=workers,
        core=core,
        handle=handle,
        source=source,
        attempt=attempt,
        native=factory.owned[attempt.id],
        authority=workers._freshness[attempt.id],
        clock=clock,
    )


def block_collection(g, monkeypatch, *, background=True):
    entered, release, exited = Event(), Event(), Event()
    prepare = g.source.prepare

    def wait(*args, **kwargs):
        entered.set()
        assert release.wait(10), "Owned fixture collector was never released"
        try:
            return prepare(*args, **kwargs)
        finally:
            exited.set()

    monkeypatch.setattr(g.source, "prepare", wait)
    if background:
        g.clock[0] += 21
        assert entered.wait(7), "Actual background collector did not reach the fixture barrier"
    return entered, release, exited


@pytest.mark.parametrize("terminal", ["done", "paused"])
def test_success_or_stop_drains_owned_collection_before_core_terminal_mutation(
    composition,
    monkeypatch,
    terminal,
):
    g = graph(composition)
    entered, release, exited = block_collection(g, monkeypatch)
    native_stopped, finished = Event(), Event()
    stop = g.native.stop
    output, errors = [], []

    def observed_stop():
        result = stop()
        if result.confirmed:
            native_stopped.set()
        return result

    monkeypatch.setattr(g.native, "stop", observed_stop)
    before = g.core.get_task(g.handle, g.task.id)
    renewals = [
        e
        for e in g.core.notifications.list_events(g.handle)
        if e.kind == "scheduling_authority_renewed"
    ]

    def finish():
        try:
            if terminal == "done":
                g.workers.execute_reviewed(
                    g.handle, g.attempt.id, "assignment", "closing-assignment"
                )
            else:
                g.workers.stop(g.handle, g.attempt.id, operation_key="closing-stop")
            output.append(g.core.get_attempt(g.handle, g.attempt.id))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    worker = Thread(target=finish)
    worker.start()
    try:
        assert entered.is_set() and native_stopped.wait(3)
        assert g.authority._stop.is_set()
        with pytest.raises(AccessDenied):
            g.authority.check_prepared()
        # This is the original failure: a0.5s join returned UNKNOWN while the
        # still-owned provider operation later observed that terminal mutation.
        assert not finished.wait(0.75)
        assert g.core.get_task(g.handle, g.task.id) == before
        assert not exited.is_set()
        release.set()
        assert finished.wait(4)
        worker.join(timeout=1)
        assert errors == []
        assert output[0].state == terminal
        assert exited.is_set() and not g.authority._thread.is_alive()
        assert g.authority._failure is None
        assert g.authority.collection_close_observations
        assert len(
            [
                e
                for e in g.core.notifications.list_events(g.handle)
                if e.kind == "scheduling_authority_renewed"
            ]
        ) == len(renewals)
        fact = g.runtime.journal.observations(g.attempt.id, "runtime_outcome")[-1]
        assert fact["quiescent"] and fact["stop_components"]["active_collector"]
        assert not fact["authority_failed_before_close"]
    finally:
        release.set()
        worker.join(timeout=4)
        g.authority._thread.join(timeout=4)


def test_deadline_exhausted_while_collector_owned_retains_unknown_and_resources(
    composition,
    monkeypatch,
):
    g = graph(composition)
    _, release, exited = block_collection(g, monkeypatch)
    try:
        g.clock[0] = g.native.binding.deadline
        outcome = g.workers.stop(g.handle, g.attempt.id, operation_key="expired-drain")
        assert outcome.state == "unknown"
        assert g.native.process._closed and not exited.is_set()
        fact = g.runtime.journal.observations(g.attempt.id, "runtime_outcome")[-1]
        assert not fact["quiescent"] and not fact["stop_components"]["active_collector"]
        with g.core.context.db.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM resource_leases WHERE attempt_id=?", (g.attempt.id,)
                ).fetchone()[0]
                == 2
            )
        release.set()
        g.authority._thread.join(timeout=4)
        assert not g.authority._thread.is_alive()
        assert g.core.get_attempt(g.handle, g.attempt.id).state == "unknown"
        assert g.authority._failure is None
    finally:
        release.set()
        g.authority._thread.join(timeout=4)


def test_close_drains_a_synchronous_collection_owner_too(composition, monkeypatch):
    g = graph(composition)
    # The monitor exits on closure, but this separate trusted caller owns the
    # collection mutex and must also finish before quiescence is confirmed.
    entered, release, exited = block_collection(g, monkeypatch, background=False)
    errors = []

    def collect():
        try:
            g.authority.refresh()
        except BaseException as error:
            errors.append(error)

    collector = Thread(target=collect)
    collector.start()
    assert entered.wait(2)
    g.authority.request_close()
    completed = Event()
    values = []

    def close():
        values.append(g.authority.close())
        completed.set()

    closer = Thread(target=close)
    closer.start()
    try:
        assert not completed.wait(0.75)
        release.set()
        assert completed.wait(3)
        collector.join(timeout=2)
        closer.join(timeout=2)
        assert values == [True] and exited.is_set()
        assert len(errors) == 1 and isinstance(errors[0], AccessDenied)
        assert g.authority._failure is None
    finally:
        release.set()
        collector.join(timeout=3)
        closer.join(timeout=3)
        g.workers.stop(g.handle, g.attempt.id, operation_key="synchronous-drain-stop")


def test_genuine_failure_observed_before_close_is_not_reclassified_as_shutdown(
    composition,
    monkeypatch,
):
    g = graph(composition)
    original = AccessDenied("Authored source failure before closure")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(g.source, "prepare", fail)
    with pytest.raises(AccessDenied) as captured:
        g.authority.refresh()
    assert captured.value is original and g.authority._failure == "AccessDenied"
    g.authority.request_close()
    # A successful SDK observation must not erase the already-latched work fault.
    dispatch = g.workers.dispatches[g.attempt.id]
    outcome = g.workers._finish(
        g.handle,
        g.attempt.id,
        dispatch.waiting,
        g.native,
        "faulted-success-stop",
        "authored terminal observation",
        "done",
    )
    assert outcome.state == "failed"
    fact = g.runtime.journal.observations(g.attempt.id, "runtime_outcome")[-1]
    assert fact["authority_failed_before_close"] and fact["quiescent"]
    assert g.authority.collection_close_observations == []


def test_hold_or_version_change_during_open_collection_still_refuses_publication(
    composition,
    monkeypatch,
):
    g = graph(composition)
    prepare = g.source.prepare

    def changed(*args, **kwargs):
        value = prepare(*args, **kwargs)
        dispatch = g.workers.dispatches[g.attempt.id]
        g.runtime.hold(dispatch)
        return value

    monkeypatch.setattr(g.source, "prepare", changed)
    with pytest.raises(StaleVersion):
        g.authority.refresh()
    assert g.authority._failure is not None
    assert g.authority.collection_close_observations == []
    task = g.core.get_task(g.handle, g.task.id)
    assert task.human_hold
    with pytest.raises(AccessDenied):
        g.authority.check_prepared()
    # Physical cleanup is independent of the preserved human hold/decision.
    g.authority.request_close()
    g.native.stop()
    g.authority.close()


def test_pre_admitted_renewal_finishes_before_close_is_confirmed(composition, monkeypatch):
    g = graph(composition)
    committed, release, closed = Event(), Event(), Event()
    renew = g.core.scheduling.renew
    results, errors = [], []

    def admitted(*args):
        value = renew(*args)
        committed.set()
        assert release.wait(4)
        return value

    monkeypatch.setattr(g.core.scheduling, "renew", admitted)

    def collect():
        try:
            g.authority.refresh()
        except BaseException as error:
            errors.append(error)

    collector = Thread(target=collect)
    collector.start()
    assert committed.wait(2)
    g.authority.request_close()

    def close():
        results.append(g.authority.close())
        closed.set()

    closer = Thread(target=close)
    closer.start()
    try:
        assert not closed.wait(0.75)
        # The admitted Core transaction already ended; an independent read works.
        count = len(
            [
                e
                for e in g.core.notifications.list_events(g.handle)
                if e.kind == "scheduling_authority_renewed"
            ]
        )
        release.set()
        assert closed.wait(3)
        collector.join(timeout=2)
        closer.join(timeout=2)
        assert results == [True] and errors == []
        with pytest.raises(AccessDenied):
            g.authority.refresh()
        assert (
            len(
                [
                    e
                    for e in g.core.notifications.list_events(g.handle)
                    if e.kind == "scheduling_authority_renewed"
                ]
            )
            == count
        )
        assert g.authority._failure is None
    finally:
        release.set()
        collector.join(timeout=3)
        closer.join(timeout=3)
        g.workers.stop(g.handle, g.attempt.id, operation_key="admitted-publication-stop")
