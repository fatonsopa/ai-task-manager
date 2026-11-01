"""Recorded-frame controls over owned Python fixtures; no provider or model call."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory, WaitingGateway
from ai_task_manager.adapters.hosts.bridge.gateway import ManagedToolGateway
from ai_task_manager.adapters.hosts.bridge.policy import NativeProfile, codex_command, policy_digest
from ai_task_manager.adapters.hosts.bridge.startup import HostStartupReceipts, StartupObserver
from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.adapters.hosts.transports.claude import sdk_command, sdk_options
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.supervisor.managed import ManagedWorkers
from tests.hosts.conftest import FixturePolicy
from tests.hosts.test_policy import policy as native_policy
from tests.runtime.helpers import RuntimeRig

PINNED = "fixture-requested-model"
OTHER = "fixture-different-model"


def claude_control(native, binding, journal, *, attempt_id=None):
    process, transcript, emit = native()
    transcript.binding = replace(
        binding, host="claude", attempt_id=attempt_id or binding.attempt_id
    )
    policy = FixturePolicy(
        binding.configuration_digest,
        process.command.cwd,
        {"CLAUDE_CONFIG_DIR": "synthetic-profile"},
        (),
    )
    policy.model = PINNED
    auth = journal._append(
        "trace",
        {
            "auth_method": "claude.ai",
            "account_hash": "synthetic-account",
            "configuration_digest": binding.configuration_digest,
            "source": "authored mechanical control, not actual subscription evidence",
        },
    )
    observer = StartupObserver(
        transcript, policy, expected_account="synthetic-account", claude_auth_ref=auth
    )

    def initialize(model=PINNED):
        return emit(
            {
                "type": "system",
                "subtype": "init",
                "model": model,
                "session_id": "synthetic-native-session",
                "apiKeySource": "none",
                "cwd": str(policy.cwd),
                "claude_code_version": "2.1.301",
                "tools": [],
                "mcp_servers": [],
            }
        )

    return process, transcript, emit, observer, initialize


def fallback():
    # The field shape is retained from the failed live pilot. Fixture model names
    # deliberately cannot be interpreted as another approved live model profile.
    return {
        "type": "system",
        "subtype": "model_refusal_fallback",
        "session_id": "synthetic-native-session",
        "original_model": PINNED,
        "fallback_model": OTHER,
    }


def test_refusal_fallback_during_neutral_poisoning_refuses_startup(native, binding, journal):
    process, _, emit, observer, initialize = claude_control(native, binding, journal)
    initialize()
    first = observer.ready_ref
    with pytest.raises(AccessDenied):
        emit(fallback())
    assert observer.ready_ref == first and observer.model == PINNED
    with pytest.raises(AccessDenied):
        HostStartupReceipts(journal, {binding.attempt_id: process}).verify_host_ready(first)


def test_changed_repeated_init_cannot_replace_original_ready_receipt(native, binding, journal):
    _, _, _, observer, initialize = claude_control(native, binding, journal)
    initialize()
    first = observer.ready_ref
    with pytest.raises(AccessDenied):
        initialize(OTHER)
    assert observer.ready_ref == first and observer.model == PINNED
    with pytest.raises(AccessDenied):
        initialize(PINNED)


def test_identical_repeated_init_preserves_receipt_and_original_expiry(native, binding, journal):
    _, _, _, observer, initialize = claude_control(native, binding, journal)
    initialize()
    first = observer.ready_ref
    original = journal._read(first)
    initialize()
    assert observer.ready_ref == first
    assert journal._read(first) == original


def gateway_control(native, binding, journal, tmp_path):
    r = RuntimeRig(tmp_path)
    dispatch = r.start()
    attempt = r.admit(dispatch)
    sid, scope = r.rig.core.session_binding(r.rig.handle)
    owned_binding = replace(
        binding,
        scope=scope,
        core_session=sid,
        attempt_id=attempt.id,
        worker_id=attempt.worker_id,
    )
    process, transcript, emit, observer, initialize = claude_control(native, owned_binding, journal)
    initialize()
    observer.admitted = True
    gateway = ManagedToolGateway(
        r.rig.core,
        r.rig.handle,
        dispatch,
        Workspace(dispatch.resources.worktree),
        r.resources,
        journal,
        deadline=journal.clock() + 30,
        model_guard=observer.model_guard,
    )
    gateway.admit()
    return r, dispatch, gateway, process, transcript, emit, observer


def edit(key="edit"):
    return {
        "path": "owned.py",
        "text": "answer = 42\n",
        "expected_hash": None,
        "operation_key": key,
    }


def test_assigned_model_change_is_rejected_before_tool_dispatch(native, binding, journal, tmp_path):
    for index, model in enumerate((OTHER, None)):
        r, dispatch, gateway, _, transcript, emit, _ = gateway_control(
            native, binding, journal, tmp_path / ("owned-" + str(index))
        )

        def route(frame, source):
            if frame.get("type") == "assistant":
                gateway.call("write_file", edit())

        transcript.listen(route)
        try:
            with pytest.raises(AccessDenied):
                emit(
                    {
                        "type": "assistant",
                        "session_id": "synthetic-native-session",
                        "message": {"model": model, "content": [{"type": "tool_use"}]},
                    }
                )
            assert not (dispatch.resources.worktree / "owned.py").exists()
        finally:
            r.close()


def test_queued_effect_after_model_fence_cannot_publish(native, binding, journal, tmp_path):
    r, dispatch, gateway, _, _, emit, _ = gateway_control(
        native, binding, journal, tmp_path / "owned"
    )
    try:
        with pytest.raises(AccessDenied):
            emit(fallback())
        with pytest.raises(AccessDenied):
            gateway.call("write_file", edit())
        assert not (dispatch.resources.worktree / "owned.py").exists()
    finally:
        r.close()


def test_restart_cannot_erase_an_observed_fallback_fence(native, binding, journal):
    process, transcript, emit, observer, initialize = claude_control(native, binding, journal)
    initialize()
    first = observer.ready_ref
    with pytest.raises(AccessDenied):
        emit(fallback())
    with pytest.raises(AccessDenied):
        StartupObserver(
            transcript,
            observer.policy,
            expected_account="synthetic-account",
            claude_auth_ref=observer.claude_auth_ref,
        )
    with pytest.raises(AccessDenied):
        HostStartupReceipts(journal, {binding.attempt_id: process}).verify_host_ready(first)


def test_explicit_model_is_bound_to_policy_digest_and_sdk_argv(tmp_path):
    p = native_policy(tmp_path, "claude")
    options = sdk_options(p, WaitingGateway())
    command = sdk_command(p, options, 10)
    assert command.argv[command.argv.index("--model") + 1] == p.model
    assert (
        policy_digest(p.host, p.executable_sha256, dict(p.environment), p.cwd, {}, model=OTHER)
        != p.configuration_digest
    )
    options.model = OTHER
    with pytest.raises(AccessDenied):
        sdk_command(p, options, 10)
    options.model = p.model
    options.fallback_model = OTHER
    with pytest.raises(AccessDenied):
        sdk_command(p, options, 10)


def test_missing_claude_model_profile_refuses_before_core_reservation(tmp_path, journal):
    r = RuntimeRig(tmp_path / "runtime")
    r.rig.capacity = CapacityPolicy("claude", 1, 2)
    r.rig.core = r.rig.new_core()
    task = r.rig.queued()
    decision = r.rig.decision(task, "In Progress")

    def forbidden_policy(host, resources):
        raise AssertionError("Missing model profile must refuse before resources exist")

    factory = ManagedHostFactory(
        r.rig.core,
        r.resources,
        journal,
        tmp_path / "native",
        forbidden_policy,
        SimpleNamespace(check_admission=lambda host: None),
        {"claude": "synthetic"},
    )
    workers = ManagedWorkers(
        r.rig.core,
        r.journal,
        r.resources,
        r.runtime,
        r.proofs,
        None,
        factory,
        deadline=r.journal.clock() + 30,
        principal_ref="synthetic",
    )
    try:
        with pytest.raises(AccessDenied, match="model profile"):
            workers.start(
                r.rig.handle,
                task.id,
                "claude",
                expected_version=task.version,
                decision_revision=task.decision_revision,
                decision_ref=decision,
                operation_key="missing-model",
            )
        with r.rig.db.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert not factory.owned and not r.runtime.workers
    finally:
        r.close()


def test_same_model_text_about_fallback_is_not_a_native_fallback_event(native, binding, journal):
    _, _, emit, observer, initialize = claude_control(native, binding, journal)
    initialize()
    observer.admitted = True
    emit(
        {
            "type": "assistant",
            "session_id": "synthetic-native-session",
            "message": {
                "model": PINNED,
                "content": [{"type": "text", "text": "Explain model_refusal_fallback."}],
            },
        }
    )
    observer.assert_stable()


def test_model_fence_and_short_publication_have_a_serialized_order(
    native, binding, journal, tmp_path
):
    early, early_dispatch, early_gateway, _, _, early_emit, _ = gateway_control(
        native, binding, journal, tmp_path / "fence-first"
    )
    try:
        with pytest.raises(AccessDenied):
            early_emit(fallback())
        with pytest.raises(AccessDenied):
            early_gateway.call("write_file", edit())
        assert not (early_dispatch.resources.worktree / "owned.py").exists()
    finally:
        early.close()
    r, dispatch, gateway, _, _, emit, _ = gateway_control(
        native, binding, journal, tmp_path / "owned"
    )
    entered, release, fence_started = Event(), Event(), Event()
    original = gateway.workspace.write

    def bounded_write(path, text, expected_hash):
        # A finite test barrier exposes ordering; production publication has no
        # event, model, command or network wait inside its short boundary.
        entered.set()
        assert release.wait(2)
        return original(path, text, expected_hash)

    gateway.workspace.write = bounded_write

    def fence():
        fence_started.set()
        with pytest.raises(AccessDenied):
            emit(fallback())

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            publication = pool.submit(gateway.call, "write_file", edit())
            assert entered.wait(2)
            refused = pool.submit(fence)
            assert fence_started.wait(1)
            assert not refused.done()
            release.set()
            publication.result(timeout=3)
            refused.result(timeout=3)
        assert (dispatch.resources.worktree / "owned.py").read_text() == "answer = 42\n"
        with pytest.raises(AccessDenied):
            gateway.call("write_file", {**edit("later"), "path": "later.py"})
        assert not (dispatch.resources.worktree / "later.py").exists()
    finally:
        release.set()
        r.close()


def test_requested_effort_is_bound_to_policy_digest_and_native_argv(tmp_path):
    for host in ("claude", "codex"):
        folder = tmp_path / host
        folder.mkdir()
        original = native_policy(folder, host)
        fingerprint = policy_digest(
            host,
            original.executable_sha256,
            dict(original.environment),
            original.cwd,
            {},
            model=PINNED,
            effort="high",
        )
        policy = replace(original, model=PINNED, effort="high", configuration_digest=fingerprint)
        assert (
            policy_digest(
                host,
                policy.executable_sha256,
                dict(policy.environment),
                policy.cwd,
                {},
                model=PINNED,
                effort="low",
            )
            != fingerprint
        )
        if host == "claude":
            options = sdk_options(policy, WaitingGateway())
            command = sdk_command(policy, options, 10)
            assert command.argv[command.argv.index("--effort") + 1] == "high"
            assert options.max_turns == 4 and options.max_budget_usd == 0.25
            options.effort = "low"
            with pytest.raises(AccessDenied):
                sdk_command(policy, options, 10)
        else:
            assert 'model_reasoning_effort="high"' in codex_command(policy, 10).argv


def test_unreported_native_effort_stays_unknown(native, binding, journal):
    _, _, _, observer, initialize = claude_control(native, binding, journal)
    observer.policy.effort = "high"
    initialize()
    payload = journal._read(observer.ready_ref, "startup")["payload"]
    assert payload["requested_effort"] == "high"
    assert payload["observed_effort"] is None


def test_factory_profile_matches_both_model_and_effort(tmp_path, journal):
    original = native_policy(tmp_path, "claude")
    configured = replace(
        original,
        model=PINNED,
        effort="high",
        configuration_digest=policy_digest(
            "claude",
            original.executable_sha256,
            dict(original.environment),
            original.cwd,
            {},
            model=PINNED,
            effort="high",
        ),
    )

    def build(host, resources):
        return configured

    factory = ManagedHostFactory(
        None,
        None,
        journal,
        tmp_path / "native",
        build,
        None,
        {"claude": "fixture"},
        model_profiles={"claude": NativeProfile(PINNED, "high")},
    )
    assert factory.configuration_digest("claude", None) == configured.configuration_digest
    for index, profile in enumerate((NativeProfile(PINNED, "low"), NativeProfile(OTHER, "high"))):
        wrong = ManagedHostFactory(
            None,
            None,
            journal,
            tmp_path / ("wrong-" + str(index)),
            build,
            None,
            {"claude": "fixture"},
            model_profiles={"claude": profile},
        )
        with pytest.raises(AccessDenied):
            wrong.configuration_digest("claude", None)
    with pytest.raises(AccessDenied):
        ManagedHostFactory(
            None,
            None,
            journal,
            tmp_path / "boolean",
            build,
            None,
            {},
            model_profiles={"claude": True},
        )
