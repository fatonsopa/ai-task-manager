"""Private real-store identity admission lifecycle; no native CLI/model effects."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding, InvocationPermits
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native.controller import NormalInvocationController
from tests.native_installation.test_normal import claim, reservation, start
from tests.native_installation.test_normal import normal as normal


def parent(normal):
    _, store, _, _, permission = normal
    c = permission.controller
    c.reserve("host", **reservation(permission))
    row = claim(c, "host")
    start(c, "host")
    binding = InvocationBinding.observed(
        {"run_id": store.binding.store_id, "policy_revision": 1, "call": row}
    )
    permit = InvocationPermits(c, {"host": binding}).prepare("host")
    return c, permission, permit


def busy_event(monkeypatch):
    from ai_task_manager.installation.native import store

    busy, original = Event(), store.is_busy_sqlite

    def observe(error):
        result = original(error)
        if result:
            busy.set()
        return result

    monkeypatch.setattr(store, "is_busy_sqlite", observe)
    return busy


@pytest.mark.parametrize("stage", ["reserve", "claim", "snapshot", "prepare", "start", "outcome"])
def test_entire_identity_admission_waits_without_replaying_bodies(normal, monkeypatch, stage):
    c, permission, owner = parent(normal)
    c.reserve("auth", **reservation(permission, "auth", "host")) if stage != "reserve" else None
    child = claim(c, "auth") if stage not in {"reserve", "claim"} else None
    child_binding = (
        InvocationBinding.observed(
            {"run_id": c.store.binding.store_id, "policy_revision": 1, "call": child}
        )
        if child
        else None
    )
    if stage == "outcome":
        start(c, "auth")
    busy, entered, done = busy_event(monkeypatch), Event(), Event()
    release, held = Event(), Event()
    calls = []

    def hold():
        with c.effect_boundary("host", worker_identity="worker"):
            held.set()
            assert release.wait(2)

    def refresh():
        with owner.issuer.identity_boundary(owner, not_after=owner.binding.deadline):
            entered.set()
            assert held.wait(1)
            calls.append(stage)
            if stage == "reserve":
                c.reserve("auth", **reservation(permission, "auth", "host"))
            elif stage == "claim":
                claim(c, "auth")
            elif stage == "snapshot":
                c.snapshot()
            elif stage == "prepare":
                InvocationPermits(c, {"auth": child_binding}).prepare_launch("auth")
            elif stage == "start":
                start(c, "auth")
            else:
                c.record_outcome(
                    "auth",
                    outcome="succeeded",
                    process_stopped=True,
                    evidence_ref="host-record:local-auth",
                    invocation=child_binding,
                )
            done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshing = pool.submit(refresh)
        assert entered.wait(1)
        holding = pool.submit(hold)
        assert held.wait(1) and busy.wait(1)
        assert not done.is_set()
        release.set()
        holding.result(timeout=1)
        refreshing.result(timeout=1)
    assert calls == [stage]
    assert len([row for row in c.snapshot()["calls"] if row["call_id"] == "auth"]) == 1


@pytest.mark.parametrize("change", ["cancel", "expiry", "owner", "permission"])
def test_identity_admission_cancels_expires_and_revalidates_original_parent(
    normal, monkeypatch, change
):
    c, permission, owner = parent(normal)
    now = [c.clock()]
    c = NormalInvocationController(c.store, c.permission, clock=lambda: now[0])
    issuer = InvocationPermits(c, {"host": owner.binding})
    owner = issuer.prepare("host")
    ready, busy, cancel = Event(), busy_event(monkeypatch), Event()
    proceed = Event()

    def refresh():
        with issuer.identity_boundary(owner, not_after=owner.binding.deadline, cancelled=cancel):
            ready.set()
            assert proceed.wait(1)
            c.reserve("auth", **reservation(permission, "auth", "host"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(refresh)
        assert ready.wait(1)
        with c.effect_boundary("host", worker_identity="worker"):
            proceed.set()
            assert busy.wait(1)
            if change == "cancel":
                cancel.set()
            elif change == "expiry":
                now[0] += timedelta(seconds=10)
            elif change == "owner":
                row = c.store.read(c.store._active.connection, "native_calls", "host")
                row["worker_identity"] = "foreign"
                c.store.put(c.store._active.connection, "native_calls", "host", row)
            else:
                row = c.store.read(c.store._active.connection, "native_permissions", c.reference)
                row["revoked"] = True
                c.store.put(c.store._active.connection, "native_permissions", c.reference, row)
            if change in {"cancel", "expiry"}:
                with pytest.raises(AccessDenied):
                    future.result(timeout=1)
        if change not in {"cancel", "expiry"}:
            with pytest.raises(AccessDenied):
                future.result(timeout=1)
    assert all(row["call_id"] != "auth" for row in c.snapshot()["calls"])


def test_identity_wait_cannot_nest_and_does_not_change_default_policy(normal):
    c, permission, owner = parent(normal)
    with c.effect_boundary("host", worker_identity="worker"):
        with pytest.raises(AccessDenied, match="own native transaction"):
            with owner.issuer.identity_boundary(owner, not_after=owner.binding.deadline):
                pytest.fail("Nested identity wait admitted")
        # Exact original failure remains a fail-fast control outside this scoped path.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            c.reserve("auth", **reservation(permission, "auth", "host"))
    assert c.clock() < datetime.fromtimestamp(owner.binding.deadline, UTC)
