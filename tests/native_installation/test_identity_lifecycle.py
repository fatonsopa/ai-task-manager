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


def test_original_bounded_identity_read_survives_brief_peer_writer_before_sdk_result(tmp_path):
    import json
    import sqlite3
    import threading
    import time
    from types import SimpleNamespace

    import anyio
    from claude_agent_sdk import ResultMessage
    from claude_agent_sdk._internal.query import Query

    from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
    from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
    from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
    from ai_task_manager.adapters.hosts.bridge.records import HostBinding, ProcessIdentity
    from ai_task_manager.adapters.hosts.transports.claude import ClaudeSession, ClaudeTransport
    from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop
    from ai_task_manager.config.files import digest
    from ai_task_manager.domains.work.models import Scope

    tmp_path.chmod(0o700)
    state = normal.__wrapped__(tmp_path)
    controller, _, permit = parent(state)
    store = controller.store
    journal = HostJournal(tmp_path / "trace.sqlite", tmp_path / "trace.key")
    events, emitted, peer_held, peer_released = (
        [],
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    refresh_done = threading.Event()

    class InertProcess:
        # Explicit in-memory process port: no start/exec/auth/model or physical stop claim.
        _closed = False
        stopping = False
        identity = SimpleNamespace(pid=1, start_identity="authored-memory-port")

        def write(self, value):
            events.append("query_written")

        def begin_shutdown(self):
            self.stopping = True
            events.append("begin_shutdown")

        def stop(self):
            self.begin_shutdown()
            self._closed = True
            events.append("memory_port_closed")
            return GroupStop(True, GroupObservation(True))

    process = InertProcess()

    class Frames:
        def __init__(self):
            self.journal = journal
            self.binding = SimpleNamespace(attempt_id="offline-refresh")
            self.index = 0

        def read(self):
            if self.index < 10:
                self.index += 1
                return {
                    "type": "system",
                    "subtype": "thinking_tokens",
                    "session_id": "offline-session",
                }
            emitted.set()
            peer_released.wait(3)
            while not process.stopping and not refresh_done.wait(0.005):
                pass
            if process.stopping:
                return None
            return {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "duration_api_ms": 1,
                "is_error": False,
                "num_turns": 1,
                "session_id": "offline-session",
                "result": "offline control",
            }

    frames = Frames()
    transport = ClaudeTransport(process, frames)
    transport.connected = True
    startup = SimpleNamespace(
        native_session="offline-session", journal=journal, binding=frames.binding
    )
    session = ClaudeSession(None, transport, startup)
    query = Query(transport, is_streaming_mode=True)
    session.client._query, session.client._transport = query, transport
    observer = object.__new__(NativeIdentityObservations)
    observer._refresh_lock = threading.Lock()
    observer._revoked = False
    observer._invocation = SimpleNamespace(native=permit)
    observer.execution_not_after = permit.binding.deadline
    identity_binding = HostBinding(
        Scope("linear", "workspace", "principal", "project", "session"),
        "offline",
        "offline-refresh",
        "worker",
        "claude",
        "offline",
        "a" * 64,
        permit.binding.deadline,
    )
    observer.native = SimpleNamespace(
        expected_account="authored-current-account",
        policy=SimpleNamespace(model="claude-opus-5-5", effort="high"),
        binding=identity_binding,
        startup=SimpleNamespace(claude_auth_ref=None),
    )
    observer._identity = ProcessIdentity(1, "authored-memory-port", 1)
    observer._session = "offline-session"
    observer._original_ref = journal._append(
        "trace", {"scope": "authored-current-identity-control"}
    )
    observer._original_digest = digest(journal._read(observer._original_ref))
    observer.original_identity_not_after = observer.execution_not_after
    observer._issued = {}
    observer.journal = journal
    preparations = [0]

    def prepared(**kwargs):
        preparations[0] += 1
        current = getattr(observer, "_current", None)
        if current is not None:
            refresh_done.set()
        end = (
            current.expires_at
            if current is not None
            else observer.execution_not_after
            if preparations[0] == 1
            else min(time.time() + 2, observer.execution_not_after - 0.01)
        )
        return SimpleNamespace(observation=SimpleNamespace(expires_at=end))

    observer.prepare = prepared
    observer._probe = SimpleNamespace(
        observe=lambda *a: journal._append(
            "trace",
            {
                "account_hash": "authored-current-account",
                "auth_method": "claude.ai",
                "configuration_digest": identity_binding.configuration_digest,
            },
            expires_at=observer.execution_not_after,
        )
    )
    native = object.__new__(ManagedNativeHost)
    native.policy = SimpleNamespace(codex_settings=None)
    native.journal, native.binding, native.process = journal, frames.binding, process
    native.failure_diagnostics, native.diagnostics_truncated = [], False
    native._stop_lock, native._physical_stop = threading.Lock(), None
    native._session_close_attempted = False
    native.session = native.portal = native._portal_context = None
    native.permits = SimpleNamespace(
        record_host_stop=lambda *a: events.append("authored_stop_port")
    )
    helper = IdentityRefresh(observer, tmp_path / "refresh", 1, on_failure=native.identity_failed)
    native.identity = native._identity_bound = helper
    owner_failure = native.identity_failed

    def failed(error):
        events.append(
            {
                "identity_error": type(error).__name__,
                "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
                "sqlite_errorname": getattr(error, "sqlite_errorname", None),
            }
        )
        owner_failure(error)

    helper._on_failure = failed

    def peer():
        assert emitted.wait(2)
        conn = sqlite3.connect(store.path)
        conn.execute("BEGIN EXCLUSIVE")
        peer_held.set()
        time.sleep(1.25)
        conn.rollback()
        conn.close()
        events.append("peer_writer_released")
        peer_released.set()

    peer_thread = threading.Thread(target=peer)

    async def consume():
        await query.start()
        peer_thread.start()
        while not peer_held.is_set():
            await anyio.sleep(0.005)
        helper.start()
        try:
            return await session.turn("offline control; no native input")
        finally:
            helper.request_stop()
            peer_thread.join(timeout=3)
            helper.close()
            query._closed = True
            await transport.close()

    try:
        result = anyio.run(consume)
        assert isinstance(result, ResultMessage)
        assert helper._failure is None
    finally:
        peer_released.set()
        if peer_thread.ident is not None:
            peer_thread.join(timeout=3)
        path = tmp_path / "connected-observation.json"
        path.write_text(
            json.dumps(
                {
                    "events": events,
                    "last_turn_count": session._last_turn_observation[1],
                    "process_stopping": process.stopping,
                    "identity_failure": helper._failure,
                    "scope": "Authored current identity and in-memory process ports; actual NativeStore/NativeIdentityObservations.refresh/IdentityRefresh/ManagedNativeHost.stop/ClaudeTransport/pinned SDK Query/ClaudeSession. No physical/native/model/provider/auth effects.",
                },
                indent=2,
            )
            + "\n"
        )


@pytest.mark.parametrize("change", ["cancel", "expiry"])
def test_preliminary_identity_snapshot_wait_keeps_cancel_and_original_expiry(
    normal, monkeypatch, change
):
    c, _, owner = parent(normal)
    busy, cancelled = busy_event(monkeypatch), Event()
    now = [c.clock()]
    c = NormalInvocationController(c.store, c.permission, clock=lambda: now[0])
    yielded = []

    def acquire():
        with c.identity_boundary(
            owner.binding, not_after=owner.binding.deadline, cancelled=cancelled
        ):
            yielded.append(True)

    peer = sqlite3.connect(c.store.path)
    try:
        peer.execute("BEGIN EXCLUSIVE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(acquire)
            assert busy.wait(1)
            if change == "cancel":
                cancelled.set()
            else:
                now[0] += timedelta(seconds=10)
            with pytest.raises(AccessDenied):
                waiting.result(timeout=1)
    finally:
        peer.rollback()
        peer.close()
    assert yielded == []


def test_preliminary_identity_snapshot_never_retries_non_busy_sqlite_error(normal, monkeypatch):
    c, _, owner = parent(normal)
    peer = sqlite3.connect(c.store.path)
    peer.execute("DROP TABLE native_meta")
    peer.commit()
    peer.close()
    seen = []
    from ai_task_manager.installation.native import store as store_module

    original = store_module.is_busy_sqlite

    def classified(error):
        seen.append(error.sqlite_errorcode)
        return original(error)

    monkeypatch.setattr(store_module, "is_busy_sqlite", classified)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with c.identity_boundary(owner.binding, not_after=owner.binding.deadline):
            pytest.fail("Malformed metadata granted identity authority")
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert seen == [sqlite3.SQLITE_ERROR]
