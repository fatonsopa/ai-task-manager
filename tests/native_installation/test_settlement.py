"""Private real-controller accounting races; no model or provider effects."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest

from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native.controller import NormalInvocationController
from ai_task_manager.installation.native.store import NativeStore
from tests.native_installation.test_normal import claim, reservation, start
from tests.native_installation.test_normal import normal as normal


def running(normal):
    _, store, _, _, permission = normal
    c = permission.controller
    c.reserve("host", **reservation(permission))
    claim(c, "host")
    start(c, "host")
    c.reserve("auth", **reservation(permission, "auth", "host"))
    row = claim(c, "auth")
    binding = InvocationBinding.observed(
        {"run_id": store.binding.store_id, "policy_revision": 1, "call": row}
    )
    start(c, "auth")
    return c, binding


def settle(c, binding, **changes):
    return c.record_outcome(
        "auth",
        outcome="succeeded",
        process_stopped=True,
        evidence_ref="host-record:authored-completion",
        invocation=binding,
        **changes,
    )


def contention(monkeypatch):
    from ai_task_manager.installation.native import store

    busy, original = Event(), store.is_busy_sqlite

    def observed(error):
        result = original(error)
        if result:
            busy.set()
        return result

    monkeypatch.setattr(store, "is_busy_sqlite", observed)
    return busy


def test_settlement_waits_for_existing_effect_then_commits(normal, monkeypatch):
    c, binding = running(normal)
    busy = contention(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with c.effect_boundary("host", worker_identity="worker"):
            future = pool.submit(settle, c, binding)
            assert busy.wait(1)
            assert not future.done()
        row = future.result(timeout=1)
    assert row["status"] == "succeeded" and row["process_stopped"]
    # Idempotent same result, but no change to settled history or stop fact.
    assert settle(c, binding)["status"] == "succeeded"
    for outcome, stopped in [("failed", True), ("succeeded", False)]:
        with pytest.raises(AccessDenied, match="history|stop"):
            c.record_outcome(
                "auth",
                outcome=outcome,
                process_stopped=stopped,
                evidence_ref="host-record:foreign",
                invocation=binding,
            )


@pytest.mark.parametrize("reason", ["cancel", "expiry"])
def test_settlement_wait_refuses_cancel_or_original_expiry(normal, monkeypatch, reason):
    c, binding = running(normal)
    now = [c.clock()]
    c = NormalInvocationController(c.store, c.permission, clock=lambda: now[0])
    busy, cancelled = contention(monkeypatch), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with c.effect_boundary("host", worker_identity="worker"):
            future = pool.submit(settle, c, binding, cancelled=cancelled)
            assert busy.wait(1)
            if reason == "cancel":
                cancelled.set()
            else:
                now[0] += timedelta(seconds=10)
            with pytest.raises(AccessDenied, match="cancelled|expired"):
                future.result(timeout=1)
    assert (
        next(row for row in c.snapshot()["calls"] if row["call_id"] == "auth")["status"]
        == "running"
    )


@pytest.mark.parametrize("change", ["worker", "store", "deadline", "scope"])
def test_foreign_binding_cannot_obtain_settlement_wait(normal, change):
    c, binding = running(normal)
    changes = {
        "worker": {"worker_identity": "foreign"},
        "store": {"run_id": str(uuid4())},
        "deadline": {"deadline": binding.deadline + 60},
        "scope": {"attempt_id": "foreign"},
    }
    with pytest.raises(AccessDenied, match="Original native settlement"):
        settle(c, replace(binding, **changes[change]))


@pytest.mark.parametrize("change", ["owner", "history", "deadline"])
def test_owner_is_revalidated_after_wait(normal, monkeypatch, change):
    c, binding = running(normal)
    busy = contention(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with c.effect_boundary("host", worker_identity="worker"):
            future = pool.submit(settle, c, binding)
            assert busy.wait(1)
            conn = c.store._active.connection
            row = c.store.read(conn, "native_calls", "auth")
            if change == "owner":
                row["worker_identity"] = "foreign"
            elif change == "history":
                row.update(status="failed", outcome="failed", process_stopped=True)
            else:
                from datetime import datetime

                end = datetime.fromisoformat(row["invocation_bound"]["execution_deadline"])
                row["invocation_bound"]["execution_deadline"] = (
                    end + timedelta(seconds=5)
                ).isoformat()
            c.store.put(conn, "native_calls", "auth", row)
        with pytest.raises(AccessDenied, match="owner, scope|history"):
            future.result(timeout=1)


def test_replaced_store_and_nested_writer_refuse(normal):
    c, binding = running(normal)
    with c.effect_boundary("host", worker_identity="worker"):
        with pytest.raises(AccessDenied, match="own native transaction"):
            settle(c, binding)
    path = c.store.path
    path.rename(path.with_suffix(".original"))
    NativeStore.create(path)
    with pytest.raises(AccessDenied, match="file identity"):
        settle(c, binding)


def test_uncontended_expired_settlement_preserves_cleanup(normal):
    c, binding = running(normal)
    c = NormalInvocationController(c.store, c.permission, clock=lambda: c_deadline)
    from datetime import UTC, datetime

    c_deadline = datetime.fromtimestamp(binding.deadline + 1, UTC)
    assert settle(c, binding)["process_stopped"]


@pytest.mark.parametrize("path", ["probe", "normal"])
def test_auth_accounting_ports_use_original_bounded_wait(normal, monkeypatch, tmp_path, path):
    from ai_task_manager.adapters.hosts.bridge.accounting import HostInvocationAccounting
    from ai_task_manager.adapters.hosts.bridge.invocations import InvocationPermits
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
    from ai_task_manager.installation.native.probe_permits import ProbePermits

    c, binding = running(normal)
    permit = InvocationPermits(c, {"auth": binding}).prepare("auth")
    journal = HostJournal(tmp_path / "host.sqlite", tmp_path / "host.key")
    completion = {"outcome": "succeeded", "group": {"confirmed_stop": True}}
    if path == "probe":
        port = object.__new__(ProbePermits)
        port.binding, port._auth_pending = object(), permit
        port.issued, port.controller, port.journal = {"auth": permit}, c, journal
        port._identity_cancelled = Event()

        def record():
            return port.record_auth(port.binding, completion)
    else:
        port = object.__new__(HostInvocationAccounting)
        port.control, port._permits = c, {"auth": permit}
        port._evidence = lambda effect, **data: journal._append("trace", {"effect": effect, **data})

        def record():
            return port._record_auth_call("auth", completion)

    busy = contention(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with c.effect_boundary("host", worker_identity="worker"):
            future = pool.submit(record)
            assert busy.wait(1)
            assert not future.done()
        future.result(timeout=1)
    row = next(row for row in c.snapshot()["calls"] if row["call_id"] == "auth")
    assert row["status"] == "succeeded" and row["process_stopped"]


@pytest.mark.parametrize(
    "effect,stopped", [("native_probe_stop", True), ("accounting_auth", False)]
)
def test_non_auth_or_unconfirmed_stop_keeps_original_short_acquisition(
    normal, tmp_path, effect, stopped
):
    import sqlite3

    from ai_task_manager.adapters.hosts.bridge.invocations import InvocationPermits
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
    from ai_task_manager.installation.native.probe_permits import ProbePermits

    c, binding = running(normal)
    permit = InvocationPermits(c, {"auth": binding}).prepare("auth")
    port = object.__new__(ProbePermits)
    port.issued, port.controller = {"auth": permit}, c
    port.journal = HostJournal(tmp_path / "host.sqlite", tmp_path / "host.key")
    # Cleanup/unknown accounting must not introduce a bounded wait in a caller
    # that may still own another transaction or source-observation guard.
    with c.effect_boundary("host", worker_identity="worker"):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            port._record(permit, "unknown", stopped, effect=effect)
