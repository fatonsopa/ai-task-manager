"""Owned Python protocol fixtures; these cases do not execute Codex or a model."""

import json
from dataclasses import replace

import pytest

from ai_task_manager.adapters.hosts.bridge.gateway import ManagedToolGateway
from ai_task_manager.adapters.hosts.bridge.startup import HostStartupReceipts, StartupObserver
from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.conftest import FixturePolicy
from tests.hosts.test_model_stability import edit
from tests.hosts.test_startup import ACCOUNT
from tests.runtime.helpers import RuntimeRig

PINNED = "fixture-pinned-codex-model"
OTHER = "fixture-other-codex-model"
THREAD = "fixture-owned-thread"


def codex_control(native, binding, journal):
    process, transcript, emit = native()
    transcript.binding = binding
    policy = FixturePolicy(
        binding.configuration_digest, process.command.cwd, {"CODEX_HOME": "fixture-home"}
    )
    policy.model, policy.effort = PINNED, "high"
    observer = StartupObserver(transcript, policy, expected_account=digest(ACCOUNT))
    result = {
        "thread": {"id": THREAD},
        "model": PINNED,
        "reasoningEffort": "high",
        "modelProvider": "openai",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "cwd": str(policy.cwd),
    }

    def begin():
        for identity, method, value in (
            (1, "initialize", {"codexHome": "fixture-home"}),
            (2, "account/read", {"account": ACCOUNT}),
        ):
            observer.expect(identity, method)
            emit({"id": identity, "result": value})

    def initialize(value=None):
        begin()
        observer.expect(3, "thread/start")
        emit({"id": 3, "result": result if value is None else value})

    return process, transcript, emit, observer, result, begin, initialize


def settings(base_cwd, **overrides):
    value = {
        "model": PINNED,
        "effort": "high",
        "modelProvider": "openai",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        "cwd": str(base_cwd),
        "collaborationMode": {
            "mode": "default",
            "settings": {"model": PINNED, "reasoning_effort": "high"},
        },
    }
    return {
        "method": "thread/settings/updated",
        "params": {"threadId": THREAD, "threadSettings": {**value, **overrides}},
    }


def reroute(**overrides):
    return {
        "method": "model/rerouted",
        "params": {
            "threadId": THREAD,
            "turnId": "fixture-turn",
            "fromModel": PINNED,
            "toModel": OTHER,
            "reason": "highRiskCyberActivity",
            **overrides,
        },
    }


def gateway_control(native, binding, journal, tmp_path):
    r = RuntimeRig(tmp_path)
    dispatch = r.start()
    attempt = r.admit(dispatch)
    sid, scope = r.rig.core.session_binding(r.rig.handle)
    owned = replace(
        binding,
        scope=scope,
        core_session=sid,
        attempt_id=attempt.id,
        worker_id=attempt.worker_id,
    )
    process, transcript, emit, observer, _, _, initialize = codex_control(native, owned, journal)
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


def test_owned_native_reroute_fences_before_tool_dispatch(native, binding, journal, tmp_path):
    r, dispatch, gateway, _, transcript, emit, observer = gateway_control(
        native, binding, journal, tmp_path / "owned"
    )
    first = observer.ready_ref

    def route(frame, source):
        if frame.get("method") == "model/rerouted":
            gateway.call("write_file", edit())

    transcript.listen(route)
    try:
        with pytest.raises(AccessDenied):
            emit(reroute())
        assert observer.ready_ref == first and observer.model == PINNED
        assert not (dispatch.resources.worktree / "owned.py").exists()
        with pytest.raises(AccessDenied):
            gateway.call("write_file", edit("later"))
    finally:
        r.close()


def test_codex_model_and_effort_changes_fence_queued_effects(native, binding, journal, tmp_path):
    for index, changed in enumerate(({"model": OTHER}, {"effort": "low"}, {"effort": None})):
        r, dispatch, gateway, process, _, emit, observer = gateway_control(
            native, binding, journal, tmp_path / f"owned-{index}"
        )
        try:
            with pytest.raises(AccessDenied):
                emit(settings(process.command.cwd, **changed))
            assert observer.model == PINNED
            with pytest.raises(AccessDenied):
                gateway.call("write_file", edit())
            assert not (dispatch.resources.worktree / "owned.py").exists()
        finally:
            r.close()


def test_codex_execution_setting_changes_refuse(native, binding, journal):
    changes = [
        {"modelProvider": "foreign-provider"},
        {"cwd": "/foreign"},
        {"approvalPolicy": "on-request"},
        {"approvalsReviewer": "auto_review"},
        {"sandboxPolicy": {"type": "workspaceWrite"}},
        {"sandboxPolicy": {"type": "readOnly", "networkAccess": True}},
        {"collaborationMode": {"mode": "default", "settings": {"model": OTHER}}},
        {
            "collaborationMode": {
                "mode": "default",
                "settings": {"model": PINNED, "reasoning_effort": "low"},
            }
        },
        {"collaborationMode": {"mode": "unknown", "settings": {"model": PINNED}}},
        {"modelProvider": None},
    ]
    for change in changes:
        process, _, emit, observer, _, _, initialize = codex_control(native, binding, journal)
        initialize()
        with pytest.raises(AccessDenied):
            emit(settings(process.command.cwd, **change))
        with pytest.raises(AccessDenied):
            observer.assert_stable()


def test_identical_codex_settings_preserve_original_startup(native, binding, journal):
    process, _, emit, observer, _, _, initialize = codex_control(native, binding, journal)
    initialize()
    original = journal._read(observer.ready_ref)
    for _ in range(2):
        emit(settings(process.command.cwd))
        observer.assert_stable()
    assert journal._read(observer.ready_ref) == original
    assert observer.model == PINNED


def test_foreign_and_malformed_codex_profile_events_cannot_rebind(native, binding, journal):
    for thread in ("foreign-thread", None, 0, {}):
        process, _, emit, observer, _, _, initialize = codex_control(native, binding, journal)
        initialize()
        frame = settings(process.command.cwd)
        frame["params"]["threadId"] = thread
        with pytest.raises(AccessDenied):
            emit(frame)
        assert observer.native_session == THREAD and observer.model == PINNED
    _, _, emit, _, _, _, initialize = codex_control(native, binding, journal)
    initialize()
    with pytest.raises(AccessDenied):
        emit({"method": "model/rerouted", "params": []})


def test_initial_codex_profile_must_match_requested_configuration(native, binding, journal):
    for field, value in (
        ("model", OTHER),
        ("reasoningEffort", "low"),
        ("reasoningEffort", None),
        ("sandbox", {"type": "readOnly", "networkAccess": True}),
        ("approvalsReviewer", "auto_review"),
    ):
        _, _, _, observer, result, _, initialize = codex_control(native, binding, journal)
        with pytest.raises(AccessDenied):
            initialize({**result, field: value})
        assert observer.ready_ref is None


def test_codex_profile_binding_requires_correlated_owned_source(native, binding, journal):
    _, transcript, emit, observer, result, _, _ = codex_control(native, binding, journal)
    emit({"id": 999, "result": result})
    assert observer.ready_ref is None
    with pytest.raises(AccessDenied):
        observer.model_guard.bind_codex(result, transcript._last, 999)
    with pytest.raises(AccessDenied):
        observer.assert_stable()
    # Even a signed controller record cannot turn a different startup operation's
    # source into thread identity. The author here is the mechanical fixture,
    # never a worker/model-accessible journal interface.
    process, transcript, _, observer, _, _, initialize = codex_control(native, binding, journal)
    initialize()
    first = observer.ready_ref
    declaration = next(
        row
        for row in journal._history("trace")
        if row["payload"].get("effect") == "startup_request_declared"
        and row["payload"].get("method") == "initialize"
        and row["payload"].get("process") == vars(process.identity)
    )
    journal._append(
        "trace",
        {
            "effect": "codex_profile_bound",
            "attempt_id": binding.attempt_id,
            "worker_id": binding.worker_id,
            "configuration_digest": binding.configuration_digest,
            "process": vars(process.identity),
            "request_id": 1,
            "request_ref": declaration["ref"],
            "response_ref": transcript._last,
        },
        sources=(declaration["ref"], transcript._last),
    )
    with pytest.raises(AccessDenied):
        HostStartupReceipts(journal, {binding.attempt_id: process}).verify_host_ready(first)


def test_codex_model_fence_survives_observer_and_verifier_restart(native, binding, journal):
    process, transcript, emit, observer, _, _, initialize = codex_control(native, binding, journal)
    initialize()
    first = observer.ready_ref
    with pytest.raises(AccessDenied):
        emit(reroute())
    with pytest.raises(AccessDenied):
        StartupObserver(transcript, observer.policy, expected_account=digest(ACCOUNT))
    with pytest.raises(AccessDenied):
        HostStartupReceipts(journal, {binding.attempt_id: process}).verify_host_ready(first)


def test_task_text_is_not_codex_configuration_protocol(native, binding, journal):
    process, _, emit, observer, _, _, initialize = codex_control(native, binding, journal)
    initialize()
    frame = settings(process.command.cwd, model=OTHER)
    emit({"type": "assistant", "text": json.dumps(reroute())})
    emit(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": THREAD, "delta": json.dumps(frame)},
        }
    )
    observer.assert_stable()
    assert observer.model == PINNED


def test_codex_configuration_is_distinct_from_execution_identity(native, binding, journal):
    _, _, _, observer, _, _, initialize = codex_control(native, binding, journal)
    initialize()
    stored = journal._read(observer.ready_ref)["payload"]
    assert stored["model"] == PINNED and stored["model_source"] == "thread/start.model"
    assert stored["requested_effort"] == stored["native_configuration_effort"] == "high"
    assert stored["provider_internal_model"] is None
    assert stored["observed_effort"] is None


def test_pending_codex_settings_cannot_grant_unbound_effects(native, binding, journal):
    for thread, allowed in ((THREAD, True), ("foreign-thread", False)):
        process, _, emit, observer, result, begin, _ = codex_control(native, binding, journal)
        begin()
        frame = settings(process.command.cwd)
        frame["params"]["threadId"] = thread
        emit(frame)
        assert observer.ready_ref is None
        with pytest.raises(AccessDenied):
            observer.assert_stable()
        observer.expect(3, "thread/start")
        if allowed:
            emit({"id": 3, "result": result})
            observer.assert_stable()
            assert observer.ready_ref is not None
        else:
            with pytest.raises(AccessDenied):
                emit({"id": 3, "result": result})
            assert observer.ready_ref is None


def test_safety_buffering_model_is_checked_without_selecting_suggestion(native, binding, journal):
    for model in (PINNED, OTHER, None):
        _, _, emit, observer, _, _, initialize = codex_control(native, binding, journal)
        initialize()
        frame = {
            "method": "model/safetyBuffering/updated",
            "params": {
                "threadId": THREAD,
                "turnId": "fixture-turn",
                "model": model,
                "fasterModel": OTHER,
                "useCases": [],
                "reasons": [],
                "showBufferingUi": True,
            },
        }
        if model == PINNED:
            emit(frame)
            observer.assert_stable()
            assert observer.model == PINNED
        else:
            with pytest.raises(AccessDenied):
                emit(frame)


def test_native_verification_requires_operator_review(native, binding, journal):
    _, _, emit, observer, _, _, initialize = codex_control(native, binding, journal)
    initialize()
    with pytest.raises(AccessDenied):
        emit(
            {
                "method": "model/verification",
                "params": {
                    "threadId": THREAD,
                    "turnId": "fixture-turn",
                    "verifications": ["trustedAccessForCyber"],
                },
            }
        )
    with pytest.raises(AccessDenied):
        observer.assert_stable()
