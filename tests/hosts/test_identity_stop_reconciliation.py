"""Owned in-memory process receipts and real helper threads; no native effects."""

import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native.probe import NativeProbeFactory


def owned():
    host = object.__new__(ManagedNativeHost)
    host._stop_lock = threading.Lock()
    host._session_close_attempted = False
    host._physical_stop = None
    host._source_audit_boundary = lambda **kwargs: nullcontext()
    host.portal = host._portal_context = None
    host.session = SimpleNamespace(close=lambda: None)
    host.binding = SimpleNamespace(attempt_id="authored")
    host.journal = SimpleNamespace(_append=lambda *args: "host-record:authored-stop")
    records, effects = [], []
    host.permits = SimpleNamespace(record_host_stop=lambda binding, stop: records.append(stop))
    host.process = SimpleNamespace(_closed=False, identity=object(), begin_shutdown=lambda: None)

    def stop():
        effects.append("physical-stop")
        host.process._closed = True
        return GroupStop(True, GroupObservation(True))

    host.process.stop = stop
    observer = object.__new__(NativeIdentityObservations)
    observer.revoke = lambda: None
    observer.execution_not_after = time.time() + 2
    observer.journal = SimpleNamespace(clock=time.time)
    helper = object.__new__(IdentityRefresh)
    helper.observations, helper._stop = observer, threading.Event()
    host.identity = host._identity_bound = helper
    return host, helper, records, effects


def test_self_stop_stays_unknown_until_foreground_proves_helper_exit():
    host, helper, records, effects = owned()
    result = []
    thread = threading.Thread(target=lambda: result.append(host.stop()))
    helper._thread = thread
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert not result[0].confirmed and result[0].reason == "identity_helper_stop_unknown"
    factory = object.__new__(NativeProbeFactory)
    factory._current = lambda *args, **kwargs: None
    factory._stop, factory._stop_entered = result[0], True
    stopped = factory.stop(object(), host)
    assert stopped.confirmed
    assert factory.stop(object(), host) is stopped
    assert effects == ["physical-stop"]
    assert [row.confirmed for row in records] == [False, True]
    host.process = SimpleNamespace(_closed=True, identity=object())
    with pytest.raises(AccessDenied, match="owner changed"):
        factory.stop(object(), host)


def test_live_helper_cannot_be_relabelled_stopped_and_unknown_can_later_reconcile():
    host, helper, records, effects = owned()
    release = threading.Event()
    helper._thread = threading.Thread(target=lambda: release.wait(2))
    helper.observations.execution_not_after = time.time() + 0.03
    helper._thread.start()
    try:
        assert not host.stop().confirmed
        assert not host.reconcile_stop().confirmed
        release.set()
        helper._thread.join(1)
        assert host.reconcile_stop().confirmed
        assert effects == ["physical-stop"]
        assert [row.confirmed for row in records] == [False, False, True]
    finally:
        release.set()
        helper._thread.join(1)


def test_missing_or_changed_physical_stop_proof_refuses():
    host, helper, _, effects = owned()
    helper._thread = None
    with pytest.raises(AccessDenied, match="proof"):
        host.reconcile_stop()
    assert host.stop().confirmed
    host.process = SimpleNamespace(_closed=True, identity=object())
    with pytest.raises(AccessDenied, match="owner changed"):
        host.reconcile_stop()
    assert effects == ["physical-stop"]


def test_accounting_failure_after_physical_stop_reconciles_without_second_signal():
    host, helper, records, effects = owned()
    helper._thread = None
    record = host.permits.record_host_stop
    host.permits.record_host_stop = lambda *args: (_ for _ in ()).throw(AccessDenied("ledger busy"))
    host._diagnose = lambda *args: None
    with pytest.raises(AccessDenied, match="ledger busy"):
        host.stop()
    host.permits.record_host_stop = record
    assert host.reconcile_stop().confirmed
    assert effects == ["physical-stop"] and len(records) == 1
