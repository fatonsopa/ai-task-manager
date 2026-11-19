"""Actual owned authored CLI controls; no official model/account acceptance."""

import hashlib
import json
import os
import platform
import stat
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.conformance import CanaryGateway, ProbeContext, cases
from ai_task_manager.adapters.hosts.bridge.conformance.profile import (
    ProfileSources,
    prepare_profile,
)
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope
from ai_task_manager.installation.native import NativeManifest, resolve
from ai_task_manager.installation.native.probe import NativeProbeFactory
from ai_task_manager.installation.native.probe_authority import NativeProbeAuthority
from ai_task_manager.installation.native.store import NativeStore
from tests.native_installation.helpers import bundle, refresh_digest


@pytest.fixture
def probe(tmp_path, request, monkeypatch):
    from tests.native_installation.helpers import temporary_linear_quota
    temporary_linear_quota(monkeypatch, tmp_path, "principal")
    b = bundle(tmp_path / "native", "claude", "claude-opus-5-5")
    store = NativeStore.create(tmp_path / "authority.sqlite")
    executable = tmp_path / "authored-cli"
    source = Path(__file__).parents[1] / "hosts/fixture_claude_bootstrap.py"
    mode = getattr(request, "param", None)
    authored = source.read_text()
    if mode in {"default", "default_wrong_config"}:
        profile = Path(b.data["home"]) / ".claude"
        profile.mkdir(mode=0o700)
        b.data["profile"] = str(profile)
        config = (
            'os.path.join(os.environ["HOME"], ".claude")'
            if mode == "default"
            else '"foreign-config"'
        )
        authored = authored.replace('os.environ["CLAUDE_CONFIG_DIR"]', config)
    executable.write_text("#!" + sys.executable + "\n" + authored)
    executable.chmod(0o700)
    b.data.update(
        authority_store=store.pointer,
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        account_hash=claude_account_hash(
            {"email": "fixture@example.invalid", "orgId": "authored-org", "userId": "authored-user"}
        ),
    )
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    pins = ProfileSources(
        {"authored-executable": (executable, b.data["executable_sha256"])},
        {"python": platform.python_version()},
    )
    profile = prepare_profile(manifest, pins, "2.1.301")
    fixture = tmp_path / "canary"
    fixture.mkdir(mode=0o700)
    info = fixture.stat()
    context = ProbeContext(
        "authored-probe",
        manifest.digest,
        profile.fingerprint,
        Scope("linear", "workspace", "principal", "project", "probe-session"),
        "claude",
        "claude-opus-5-5",
        "high",
        b.data["account_hash"],
        time.time() + 35,
        fixture,
        (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)),
        ("native_shell_absent",),
    )
    authority = NativeProbeAuthority.enroll(
        store,
        manifest,
        pins,
        context,
        {"host": 35, "relay": 35, "auth": 5, "neutral": 8, "assignment": 8},
        identity_refreshes=("identity-1",),
        profile=profile,
    )
    journal = HostJournal(tmp_path / "host.sqlite", tmp_path / "host.key")
    factory = NativeProbeFactory(authority, journal)
    try:
        yield b, authority, context, journal, factory
    finally:
        native = factory.native
        if native is not None and native.process is not None and not native.process._closed:
            stopped = factory.stop(context, native)
            assert stopped.confirmed
        report = os.environ.get("NATIVE_INSTALL_RESOURCE_REPORT")
        if report and native is not None and native.process is not None:
            identity = asdict(native.process.identity)
            with Path(report).open("a") as out:
                out.write(
                    json.dumps(
                        {
                            "actual_authored_native": identity,
                            "closed": native.process._closed,
                            "stop": asdict(factory._stop) if factory._stop else None,
                        }
                    )
                    + "\n"
                )


def test_P301_actual_accounted_canary_start_turn_stop_without_core(probe):
    _, authority, context, journal, factory = probe
    gateway = CanaryGateway(context, authority, journal)
    native = factory.start(context, gateway)
    assert native.core is None and native.resources is None and native.commands is None
    assert native.gateway.delegate is None and not native.startup.admitted
    with pytest.raises(AccessDenied, match="normal Core"):
        native.permits.check_admission("claude")
    gateway.bind(native)
    native.gateway.delegate = gateway
    case = next(c for c in cases(context) if c.case_id == "native_shell_absent")
    gateway.begin(case)
    turn = factory.execute(context, native, case)
    request = journal._read(turn.request_ref, "trace")["payload"]
    terminal = journal._read(turn.terminal_ref, "trace")["payload"]["frame"]
    assert request["effect"] == "native_probe_submission" and request["case_id"] == case.case_id
    assert terminal["type"] == "result" and terminal["is_error"] is False
    assert gateway.end() == ()
    with pytest.raises(AccessDenied):
        factory.start(context, gateway)
    gateway.begin(case)
    with pytest.raises(AccessDenied, match="cannot be repeated"):
        factory.execute(context, native, case)
    gateway.end()
    identity = native.identity.observations
    previous = identity.prepare()
    identity.refresh(native.directory / "manual-account-refresh")
    previous.check()
    refreshed = [
        row
        for row in authority.controller.snapshot()["calls"]
        if row["call_id"].endswith(":identity-1")
    ]
    assert len(refreshed) == 1 and refreshed[0]["process_stopped"]
    stopped = factory.stop(context, native)
    assert stopped.confirmed and native.process._closed
    assert not native.identity._thread.is_alive()
    assert factory.stop(context, native) is stopped


@pytest.mark.parametrize("probe", ["default", "default_wrong_config"], indirect=True)
def test_default_profile_authentication_stays_bound(probe, request):
    _, authority, context, journal, factory = probe
    gateway = CanaryGateway(context, authority, journal)
    default_matches = request.node.callspec.params["probe"] == "default"
    if default_matches:
        native = factory.start(context, gateway)
        assert "CLAUDE_CONFIG_DIR" not in native.policy.environment
        assert factory.stop(context, native).confirmed
    else:
        with pytest.raises(AccessDenied, match="approved subscription"):
            factory.start(context, gateway)
        assert factory.native.process is None
    calls = authority.controller.snapshot()["calls"]
    expected = {"host", "relay", "auth"}
    if default_matches:
        expected.add("neutral")
    assert {row["metadata"]["invocation_binding"]["purpose"] for row in calls} == expected
    assert all(row["process_stopped"] for row in calls)


def test_failed_probe_initialization_retains_entered_launch(probe, monkeypatch):
    from ai_task_manager.adapters.hosts.bridge.auth_probe import ClaudeAuthProbe

    _, authority, context, journal, factory = probe

    def interrupted_auth(*_):
        native = factory.native
        with authority.controller.launch_boundary(
            native.permits.initialization.native.reference,
            worker_identity=native.binding.worker_id,
        ):
            raise AccessDenied("Declared interruption after launch entry")

    monkeypatch.setattr(ClaudeAuthProbe, "observe", interrupted_auth)
    with pytest.raises(AccessDenied, match="Declared interruption"):
        factory.start(context, CanaryGateway(context, authority, journal))
    calls = authority.controller.snapshot()["calls"]
    assert len(calls) == 2 and all(not row["process_stopped"] for row in calls)
    assert any(row["launch_token"] is not None for row in calls)


@pytest.mark.parametrize("fault", ["context", "fixture", "expiry", "account", "profile"])
def test_P302_wrong_probe_context_refuses_before_start(probe, fault):
    _, authority, context, journal, factory = probe
    if fault == "fixture":
        context.fixture_root.chmod(0o755)
        wrong = context
    elif fault == "context":
        wrong = replace(context)
    elif fault == "expiry":
        wrong = replace(context, deadline=time.time() - 1)
    elif fault == "account":
        wrong = replace(context, account_hash="0" * 64)
    else:
        wrong = replace(context, profile_digest="0" * 64)
    with pytest.raises(AccessDenied):
        factory.start(wrong, object())
    assert factory.native is None and authority.controller.snapshot()["calls"] == []
    assert journal._intents() == []


def test_P303_probe_permission_never_resolves_as_normal(probe):
    b, authority, context, _, _ = probe
    from ai_task_manager.installation.native.normal import NormalAuthoritySource

    # Normal source still requires its normal reference and exact normal kind.
    source = NormalAuthoritySource(authority.manifest)
    with pytest.raises(AccessDenied):
        source.resolve(authority.reference, authority.manifest)
    assert context.reference not in source.store.permission(authority.reference).get("kind", "")
    with pytest.raises(AccessDenied):
        resolve(b.config, b.installation, authority_source=source)


def test_P304_fresh_authority_cannot_rebind_enrolled_scope(probe):
    _, authority, context, _, _ = probe
    wrong = replace(context, scope=replace(context.scope, logical_session="foreign"))
    with pytest.raises(AccessDenied, match="enrolled permission"):
        NativeProbeAuthority(
            authority.store, authority.manifest, authority.pins, wrong, profile=authority.profile
        )


def test_P305_real_store_managed_read_inside_actual_assignment(probe, monkeypatch):
    """Exercise real accounting SQL and canary guards, not a null effect boundary."""
    _, authority, context, journal, factory = probe
    gateway = CanaryGateway(context, authority, journal)
    native = factory.start(context, gateway)
    gateway.bind(native)
    native.gateway.delegate = gateway
    case = next(c for c in cases(context) if c.case_id == "native_shell_absent")
    gateway.begin(case)
    original = native.portal.call
    reads = []

    def turn_and_managed_read(function, *args, **kwargs):
        result = original(function, *args, **kwargs)
        if function == native.session.turn:
            reads.append(gateway.call("read_file", {"path": "input.txt"}))
        return result

    monkeypatch.setattr(native.portal, "call", turn_and_managed_read)
    factory.execute(context, native, case)
    assert len(reads) == 1
    assert reads[0]["text"]
    events = gateway.end()
    assert len(events) == 1
    payload = journal._read(events[0], "trace")["payload"]
    assert payload["tool"] == "read_file" and payload["outcome"] == "succeeded"
    assert payload["before"] == payload["after"]


def test_P306_revoked_real_store_refuses_managed_read(probe):
    _, authority, context, journal, factory = probe
    gateway = CanaryGateway(context, authority, journal)
    native = factory.start(context, gateway)
    gateway.bind(native)
    native.gateway.delegate = gateway
    case = next(c for c in cases(context) if c.case_id == "native_shell_absent")
    gateway.begin(case)
    before = gateway.snapshot()
    authority.store.revoke(authority.reference)
    with pytest.raises(AccessDenied, match="revoked"):
        gateway.call("read_file", {"path": "input.txt"})
    assert gateway.snapshot() == before
    assert gateway.end() == ()


def test_P307_permission_read_reuses_only_own_thread_transaction_and_clears(probe):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    _, authority, _, _, _ = probe
    store = authority.store
    other = NativeStore(store.path, store.pointer)
    with store.transaction() as conn:
        assert store.permission(authority.reference)["reference"] == authority.reference
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.permission(authority.reference)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                pool.submit(store.permission, authority.reference).result(timeout=2)
        assert conn.in_transaction
    assert getattr(store._active, "connection", None) is None
    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction():
            assert store.permission(authority.reference)["reference"] == authority.reference
            raise RuntimeError("rollback")
    assert getattr(store._active, "connection", None) is None
    assert store.permission(authority.reference)["reference"] == authority.reference


@pytest.mark.parametrize("refresh", [True, False])
def test_P308_repeated_init_uses_only_successful_owned_refresh(probe, refresh):
    _, authority, context, journal, factory = probe
    gateway = CanaryGateway(context, authority, journal)
    # Shorten the first observation's virtual window without changing any
    # retained record, original native deadline or production receipt lifetime.
    journal.clock = lambda: time.time() - 40
    native = factory.start(context, gateway)
    journal.clock = time.time
    original_auth = native.startup.claude_auth_ref
    original_startup = journal._read(native.startup.ready_ref, fresh=False)
    expiry = journal._read(original_auth)["expires_at"]
    if refresh:
        native.identity.observations.refresh(native.directory / "repeat-init-auth")
    gateway.bind(native)
    native.gateway.delegate = gateway
    case = next(c for c in cases(context) if c.case_id == "native_shell_absent")
    gateway.begin(case)
    journal.clock = lambda: expiry + 0.1
    if refresh:
        turn = factory.execute(context, native, case)
        assert journal._read(turn.terminal_ref)["payload"]["frame"]["is_error"] is False
        assert native.startup.claude_auth_ref != original_auth
        assert journal._read(native.startup.ready_ref, fresh=False) == original_startup
    else:
        with pytest.raises(AccessDenied, match="stale|expired"):
            factory.execute(context, native, case)
    gateway.end()
    journal.clock = time.time
