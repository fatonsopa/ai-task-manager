"""Actual owned kernel handles plus labelled refusal controls; no host/model call."""

import select
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.transports import native_exit
from ai_task_manager.adapters.hosts.transports.native_exit import NativeExitWatch
from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain


class QueueProbe:
    def __init__(self, queue, *, action=None):
        self.queue, self.action, self.calls = queue, action, []

    @property
    def closed(self):
        return self.queue.closed

    def control(self, changes, maximum, timeout):
        self.calls.append((changes, maximum, timeout))
        if self.action is not None:
            return self.action(changes, maximum, timeout)
        return self.queue.control(changes, maximum, timeout)

    def close(self):
        self.queue.close()


def check(process):
    process.native_exit.check_current(
        process.identity, process.process.pid, process.group.start_identity
    )


def test_live_owned_generation_has_nonblocking_current_observation(native, monkeypatch):
    process, _, _ = native()
    watch = process.native_exit
    assert watch.generation.identity == process.identity
    probe = QueueProbe(watch._queue)
    monkeypatch.setattr(watch, "_queue", probe)

    def forbidden():
        raise AssertionError("Admission cannot perform a new process inspection")

    monkeypatch.setattr(process, "verify_identity", forbidden)
    process.check_current_liveness()
    assert probe.calls == [(None, 2, 0)]
    assert not watch.closed


def test_exit_is_sticky_and_does_not_rebind_a_pid(native, monkeypatch):
    process, _, _ = native()
    watch = process.native_exit
    assert process.group.stop().confirmed
    until = time.monotonic() + 3
    while True:
        try:
            check(process)
        except AccessDenied:
            break
        assert time.monotonic() < until, "Actual registered exit was not observed"
        time.sleep(0.01)
    # Authored refusal control: no constructor may reopen the observed PID.
    monkeypatch.setattr(select, "kqueue", lambda: pytest.fail("Exited PID cannot be rebound"))
    with pytest.raises(AccessDenied):
        check(process)
    assert watch._exited


@pytest.mark.parametrize("change", ["identity", "actual_exit_during_registration"])
def test_registration_gap_or_identity_change_refuses(native, monkeypatch, change):
    process, _, _ = native()
    original_verify = process.verify_identity
    factory = select.kqueue
    created = []
    calls = []

    def verify():
        value = original_verify()
        calls.append(value)
        return replace(value, pid=value.pid + 100000) if len(calls) == 2 else value

    def queue():
        real = factory()

        def action(changes, maximum, timeout):
            result = real.control(changes, maximum, timeout)
            if changes is not None and change == "actual_exit_during_registration":
                assert process.group.stop().confirmed
            return result

        probe = QueueProbe(real, action=action)
        created.append(probe)
        return probe

    if change == "identity":
        monkeypatch.setattr(process, "verify_identity", verify)
    monkeypatch.setattr(select, "kqueue", queue)
    with pytest.raises((AccessDenied, RuntimeUncertain)):
        NativeExitWatch.capture(process)
    assert created and all(item.closed for item in created)


@pytest.mark.parametrize(
    "mode", ["unsupported", "registration_error", "event_error", "foreign", "closed", "missing"]
)
def test_unknown_kernel_error_or_closed_watch_refuses(native, monkeypatch, mode):
    process, _, _ = native()
    watch = process.native_exit
    if mode == "unsupported":
        with monkeypatch.context() as scoped:
            scoped.setattr(native_exit.sys, "platform", "unsupported-fixture-platform")
            with pytest.raises(RuntimeUncertain):
                native_exit.require_exit_monitoring()
    elif mode == "registration_error":

        def denied():
            raise PermissionError("Authored kernel permission refusal")

        monkeypatch.setattr(select, "kqueue", denied)
        with pytest.raises(RuntimeUncertain):
            NativeExitWatch.capture(process)
    elif mode in {"event_error", "foreign"}:
        event = SimpleNamespace(
            ident=process.identity.pid if mode == "event_error" else process.identity.pid + 100000,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ERROR if mode == "event_error" else 0,
            fflags=select.KQ_NOTE_EXIT,
        )
        probe = QueueProbe(watch._queue, action=lambda *_: [event])
        monkeypatch.setattr(watch, "_queue", probe)
        with pytest.raises(RuntimeUncertain):
            check(process)
        probe.action = lambda *_: []
        with pytest.raises(RuntimeUncertain):
            check(process)
    elif mode == "closed":
        watch.close()
        with pytest.raises(RuntimeUncertain):
            check(process)
    else:
        with monkeypatch.context() as scoped:
            scoped.setattr(process, "native_exit", None)
            with pytest.raises(RuntimeUncertain):
                process.check_current_liveness()


def test_generation_checks_retain_parent_without_reaping(native, monkeypatch):
    process, _, _ = native()

    def forbidden(*args, **kwargs):
        raise AssertionError("Kernel observation must retain the unreaped parent anchor")

    with monkeypatch.context() as scoped:
        scoped.setattr(process.process, "poll", forbidden)
        scoped.setattr(process.process, "wait", forbidden)
        check(process)
    assert process.process.returncode is None


def test_unknown_stop_retains_watch_and_confirmed_stop_closes_it(native, monkeypatch):
    process, _, _ = native()
    watch = process.native_exit
    with monkeypatch.context() as scoped:
        scoped.setattr(
            process.group,
            "stop",
            lambda: GroupStop(False, GroupObservation(False, reason="authored_unknown")),
        )
        assert not process.stop().confirmed
        assert not watch.closed and not process._closed
    assert process.stop().confirmed
    assert watch.closed and process._closed
