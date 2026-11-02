"""Authored native frames over actual local pipes, not actual Codex acceptance."""

from copy import deepcopy

import pytest

from ai_task_manager.adapters.hosts.bridge.policy import clean_environment
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.codex_configuration_helpers import effective, observer, policy
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start


def test_trusted_codex_environment_excludes_all_auth_and_provider_fallbacks(tmp_path, monkeypatch):
    import os
    import pwd

    secrets = (
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_IDENTITY_TOKEN_FILE",
        "CODEX_API_KEY",
    )
    for key in secrets:
        monkeypatch.setenv(key, "fixture-secret")
    monkeypatch.setenv("USER", "impostor")
    p = policy(tmp_path)
    env = clean_environment(p.environment["HOME"], codex_home=p.environment["CODEX_HOME"])
    assert set(env) == {"PATH", "HOME", "USER", "LANG", "CODEX_HOME"}
    assert env["USER"] == pwd.getpwuid(os.getuid()).pw_name
    assert not set(secrets) & env.keys()


def test_supported_exclusions_are_explicit_immutable_and_private(tmp_path):
    p = policy(tmp_path, config='[mcp_servers."foreign"]\ncommand="unused-fixture"\n')
    args = p.codex_settings.arguments()
    assert 'mcp_servers."foreign".enabled=false' in args
    for key in ("hooks", "goals", "image_generation", "view_image", "plugins", "shell_tool"):
        assert f"features.{key}=false" in args
    assert not any("forced_login_method" in value for value in args)
    assert "--ignore-user-config" not in args and "disabledPluginIds" not in str(args)
    original = p.codex_settings.fingerprint
    (p.codex_settings.sources.home / "config.toml").write_text("# changed")
    with pytest.raises(AccessDenied):
        p.check()
    assert original


def test_effective_native_configuration_is_positive_and_correlated_before_thread(
    native, binding, journal, tmp_path
):
    p, _, _, _, startup, cfg, _, reply, ready, _ = observer(
        native, binding, journal, tmp_path / "good"
    )
    with pytest.raises(AccessDenied):
        cfg.pre_thread()
    ready()
    assert startup.ready_ref is None and cfg.account_observed
    broken = effective(p)
    del broken["config"]["features"]["hooks"]
    with pytest.raises(AccessDenied):
        reply("config/read", broken, {"includeLayers": True, "cwd": str(p.cwd)})


def test_managed_requirements_cannot_silently_override_the_profile(
    native, binding, journal, tmp_path
):
    for index, req in enumerate(
        (
            {},
            {"requirements": {"featureRequirements": {"hooks": True}}},
            {"requirements": {"hooks": {"unknown": True}}},
        )
    ):
        _, _, _, _, _, cfg, _, reply, _, _ = observer(
            native, binding, journal, tmp_path / str(index)
        )
        with pytest.raises(AccessDenied):
            reply("configRequirements/read", req)
        assert not cfg.inventory_complete


def test_complete_thread_mcp_inventory_is_required_before_model_admission(
    native, binding, journal, tmp_path
):
    _, _, _, _, startup, cfg, _, reply, ready, thread = observer(native, binding, journal, tmp_path)
    ready()
    thread()
    assert startup.ready_ref is None
    params = {"threadId": cfg.thread_id, "detail": "full", "cursor": None}
    reply("mcpServerStatus/list", {"data": [], "nextCursor": "next"}, params)
    assert startup.ready_ref is None
    with pytest.raises(AccessDenied):
        reply(
            "mcpServerStatus/list", {"data": [], "nextCursor": "next"}, {**params, "cursor": "next"}
        )
    with pytest.raises(AccessDenied):
        cfg.ready()


def test_loaded_instruction_sources_are_observed_not_assumed_from_a_flag(
    native, binding, journal, tmp_path
):
    _, _, _, _, startup, cfg, _, reply, ready, _ = observer(native, binding, journal, tmp_path)
    ready()
    result = {
        "thread": {"id": "fixture"},
        "model": "synthetic-no-model",
        "reasoningEffort": "high",
        "modelProvider": "openai",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"type": "readOnly"},
        "cwd": str(startup.policy.cwd),
    }
    with pytest.raises(AccessDenied):
        reply("thread/start", result)
    assert startup.ready_ref is None and cfg.thread_id is None


def test_configuration_and_inventory_traces_omit_arbitrary_secret_values(
    native, binding, journal, tmp_path
):
    p, _, transcript, _, _, _, _, reply, _, _ = observer(native, binding, journal, tmp_path)
    result = effective(p)
    secret = "fixture-random-header-secret"
    result["config"]["mcp_servers"] = {
        "foreign": {
            "enabled": False,
            "env": {"RANDOM_NAME": secret},
            "http_headers": {"Different-Key": secret},
        }
    }
    result["layers"][0]["config"] = deepcopy(result["config"])
    with pytest.raises(AccessDenied):
        reply("config/read", result, {"includeLayers": True, "cwd": str(p.cwd)})
    stored = journal._read(transcript._last)["payload"]
    assert stored["frame_kind"] == "configuration_projection"
    assert stored["raw_frame_hash"] != stored["frame_hash"]
    assert secret not in str(journal._history("trace"))


def test_native_account_or_source_change_fences_effects_until_fresh_observation(
    native, binding, journal, tmp_path, composition
):
    p, _, _, emit, startup, cfg, _, reply, ready, thread = observer(
        native, binding, journal, tmp_path
    )
    ready()
    thread()
    reply(
        "mcpServerStatus/list",
        {"data": [], "nextCursor": None},
        {"threadId": cfg.thread_id, "detail": "full"},
    )
    assert startup.ready_ref is not None
    refresh = p.codex_settings.sources.home / "refresh"
    refresh.write_text("authored-safe-refresh-not-a-credential")
    refresh.chmod(0o600)
    refresh.replace(p.codex_settings.sources.home / "auth.json")
    with pytest.raises(AccessDenied):
        startup.assert_stable()
    with pytest.raises(AccessDenied):
        emit({"method": "account/updated", "params": {"authMode": "chatgpt"}})
    with pytest.raises(AccessDenied):
        startup.assert_stable()
    (p.codex_settings.sources.home / "config.toml").write_text("# changed")
    with pytest.raises(AccessDenied):
        p.check()
    value = composition()
    attempt = start(value)
    actual = value[4].owned[attempt.id]
    (actual.policy.codex_settings.sources.home / "auth.json").write_text("fixture-cache-change")
    with pytest.raises(AccessDenied):
        actual.gateway.call(
            "write_file",
            {
                "path": "after-configuration-fence.py",
                "text": "forbidden",
                "expected_hash": None,
                "operation_key": "configuration-fence",
            },
        )
    assert not (actual.pair.worktree / "after-configuration-fence.py").exists()


def test_real_production_composition_keeps_unverified_tool_modes_refused(composition):
    value = composition()
    factory = value[4]
    original = factory.permits.require_native_conformance

    def refuse(policy, binding):
        raise AccessDenied("No real native-tool conformance observation exists")

    factory.permits.require_native_conformance = refuse
    with pytest.raises(AccessDenied):
        start(value)
    assert not value[5].calls
    for native in factory.owned.values():
        assert native.process._closed
        assert native.startup.ready_ref is None
    factory.permits.require_native_conformance = original
