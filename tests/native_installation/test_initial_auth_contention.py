"""Initial authentication over real normal permissions; no process/model effects."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict
from datetime import timedelta
from threading import Event

import pytest

from ai_task_manager.adapters.hosts.bridge.auth_probe import ClaudeAuthProbe
from ai_task_manager.domains.work.errors import AccessDenied

from ai_task_manager.config.files import write_private
from ai_task_manager.installation.native.manifest import NativeManifest, installation_intent
from ai_task_manager.installation.native.normal import NormalAuthoritySource, enroll
from ai_task_manager.installation.native.store import NativeStore
from tests.native_installation.helpers import bundle, refresh_digest
from tests.native_installation.test_normal import AuthoredRestrictions, reservation
from tests.native_installation.test_normal import actual_accounting_graph as actual_accounting_graph
from tests.native_installation.test_identity_lifecycle import busy_event


def peer_role(tmp_path, store, scope):
    peer = NativeStore(store.path, store.pointer)
    b = bundle(tmp_path / "peer", "claude", "claude-opus-5-5", task_id="peer-reviewer")
    data = json.loads(b.installation.read_text())
    data.update(scope=asdict(scope), role="reviewer")
    write_private(b.installation, data, replace=True)
    b.data.update(authority_store=peer.pointer, installation=installation_intent(b.installation),
                  authorization_ref="peer-reviewer-permission")
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    enrolled = enroll(peer, manifest, {"host": 30, "relay": 30, "auth": 5, "neutral": 8, "assignment": 8},
                      identity_refreshes=(), conformance_ref="authored-role-control", role="reviewer")
    permission = NormalAuthoritySource(manifest, conformance=AuthoredRestrictions(), role="reviewer").resolve(enrolled["reference"], manifest)
    c = permission.controller
    c.reserve("peer-host", **reservation(permission))
    c.claim_launch("peer-host", worker_identity="peer-worker", not_after=c.clock() + timedelta(seconds=10))
    with c.launch_boundary("peer-host", worker_identity="peer-worker") as report:
        report(process_pid=None, process_start_identity=None)
    return peer, c


def test_initial_auth_admission_uses_original_finite_parent_under_peer_role_writer(actual_accounting_graph, tmp_path):
    authority, policy, binding, _, store, _ = actual_accounting_graph
    accounting = authority.launch_permits
    initialization = accounting.authorize_initialization(policy, binding)
    peer, controller = peer_role(tmp_path, store, binding.scope)
    assert peer is not store and peer.path == store.path
    entered, finished = Event(), Event()

    def authenticate():
        entered.set()
        boundary = getattr(accounting, "authentication_boundary", None)
        with boundary(policy, binding) if boundary else nullcontext():
            permit = accounting.authorize_auth(policy, binding)
        finished.set()
        return permit

    with ThreadPoolExecutor(max_workers=1) as pool:
        with controller.effect_boundary("peer-host", worker_identity="peer-worker"):
            future = pool.submit(authenticate)
            assert entered.wait(1)
            # The original failure occurs before the auth child is reserved.
            if finished.wait(0.25) or future.done():
                future.result(timeout=1)
            assert not finished.is_set()
        permit = future.result(timeout=2)
    assert permit.binding.purpose == "auth"
    assert permit.binding.deadline <= initialization.native.binding.deadline
    calls = accounting.control.snapshot()["calls"]
    assert len([c for c in calls if c["metadata"]["invocation_binding"]["purpose"] == "auth"]) == 1


def test_actual_auth_probe_consumer_keeps_scope_through_launch_and_settlement(actual_accounting_graph, monkeypatch, tmp_path):
    authority, policy, binding, journal, _, _ = actual_accounting_graph
    accounting = authority.launch_permits
    accounting.authorize_initialization(policy, binding)
    stages = []

    def controlled_observation(probe, given_policy, given_binding, directory):
        # The actual consumer enters the scope. No physical auth command or
        # account success is claimed by these explicit zero-process callbacks.
        assert accounting._authentication_window is not None
        end = accounting._authentication_window[1]
        permit = accounting.authorize_auth(given_policy, given_binding)
        stages.append("authorize")
        assert permit.binding.deadline <= end
        with permit.launch_boundary() as report:
            report(process_pid=None, process_start_identity=None)
        stages.append("launch")
        accounting.record_auth(given_binding, {
            "outcome": "succeeded",
            "group": {"confirmed_stop": True, "observation": {"known": True, "live_pids": []}},
        })
        stages.append("settle")
        return "host-record:authored-observation-control"

    monkeypatch.setattr(ClaudeAuthProbe, "_observe", controlled_observation)
    result = ClaudeAuthProbe(journal, accounting).observe(policy, binding, tmp_path)
    assert result == "host-record:authored-observation-control"
    assert stages == ["authorize", "launch", "settle"]
    assert accounting._authentication_window is None
    auth = next(c for c in accounting.control.snapshot()["calls"]
                if c["metadata"]["invocation_binding"]["purpose"] == "auth")
    assert auth["status"] == "succeeded" and auth["process_stopped"]


@pytest.mark.parametrize("change", ["cancel", "expiry", "permission", "foreign_parent"])
def test_initial_auth_wait_preserves_cancel_expiry_revocation_and_parent(actual_accounting_graph, tmp_path, monkeypatch, change):
    from ai_task_manager.installation.native import controller as controller_module

    authority, policy, binding, _, store, _ = actual_accounting_graph
    accounting = authority.launch_permits
    initialization = accounting.authorize_initialization(policy, binding)
    _, peer = peer_role(tmp_path, store, binding.scope)
    busy, cancelled = busy_event(monkeypatch), Event()
    original_datetime = controller_module.datetime
    offset = [timedelta()]

    class Clock(original_datetime):
        @classmethod
        def now(cls, tz=None):
            return original_datetime.now(tz) + offset[0]

    monkeypatch.setattr(controller_module, "datetime", Clock)

    def authenticate():
        with accounting.authentication_boundary(policy, binding, cancelled=cancelled):
            return accounting.authorize_auth(policy, binding)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with peer.effect_boundary("peer-host", worker_identity="peer-worker"):
            future = pool.submit(authenticate)
            assert busy.wait(1)
            if change == "cancel":
                cancelled.set()
            elif change == "expiry":
                offset[0] = timedelta(seconds=40)
            else:
                conn = peer.store._active.connection
                table, key = (("native_permissions", accounting.control.reference)
                              if change == "permission" else ("native_calls", initialization.native.reference))
                row = peer.store.read(conn, table, key)
                row["revoked" if change == "permission" else "worker_identity"] = True if change == "permission" else "foreign"
                peer.store.put(conn, table, key, row)
            if change in {"cancel", "expiry"}:
                with pytest.raises(AccessDenied):
                    future.result(timeout=1)
        if change not in {"cancel", "expiry"}:
            with pytest.raises(AccessDenied):
                future.result(timeout=1)
    offset[0] = timedelta()
    assert accounting._authentication_window is None
    with store.settlement_snapshot() as conn:
        assert conn.execute("SELECT COUNT(*) FROM native_calls WHERE permission_ref=? AND purpose='auth'",
                            (accounting.control.reference,)).fetchone()[0] == 0


@pytest.mark.parametrize("change", ["revoke", "expiry", "cancel"])
def test_initial_auth_known_child_stop_survives_admission_loss(actual_accounting_graph, monkeypatch, tmp_path, change):
    import subprocess
    import sys

    from ai_task_manager.adapters.hosts.bridge.invocations import spawn_claimed
    from ai_task_manager.adapters.hosts.bridge import accounting as accounting_module
    from ai_task_manager.adapters.process.groups import OwnedProcessGroup, supervise
    from ai_task_manager.installation.native import controller as controller_module

    authority, policy, binding, _, store, _ = actual_accounting_graph
    accounting = authority.launch_permits
    accounting.authorize_initialization(policy, binding)
    cancelled = Event()
    original_datetime = controller_module.datetime
    offset = [timedelta()]

    class Clock(original_datetime):
        @classmethod
        def now(cls, tz=None):
            return original_datetime.now(tz) + offset[0]

    monkeypatch.setattr(controller_module, "datetime", Clock)
    monkeypatch.setattr(accounting_module, "datetime", Clock)
    child = group = None
    try:
        with pytest.raises(AccessDenied):
            with accounting.authentication_boundary(policy, binding, cancelled=cancelled):
                permit = accounting.authorize_auth(policy, binding)
                child = spawn_claimed(permit, [sys.executable, "-I", "-c", "import time;time.sleep(20)"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
                group = OwnedProcessGroup(child)
                assert group.capture().known
                if change == "revoke":
                    NativeStore(store.path, store.pointer).revoke(accounting.control.reference)
                elif change == "expiry":
                    offset[0] = timedelta(seconds=40)
                else:
                    cancelled.set()
                completion = supervise(group, timeout_seconds=0)
                assert completion["group"]["confirmed_stop"] is True
                accounting.record_auth(binding, completion)
                # Admission remains refused after the separate known-stop fact.
                with pytest.raises(AccessDenied):
                    with permit.launch_boundary():
                        pytest.fail("Stopped auth child launched again")
    finally:
        offset[0] = timedelta()
        if group is not None and child.poll() is None:
            assert group.stop().confirmed
            group.reap_exited_leader()
    assert child is not None and child.returncode is not None
    with store.settlement_snapshot() as conn:
        rows = conn.execute("SELECT data FROM native_calls WHERE permission_ref=? AND purpose='auth'",
                            (accounting.control.reference,)).fetchall()
    assert len(rows) == 1
    stopped = json.loads(rows[0][0])
    assert stopped["process_stopped"] is True, stopped
    assert stopped["outcome"] == "interrupted" and stopped["evidence"].startswith("host-record:")
