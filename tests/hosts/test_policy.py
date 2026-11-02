import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from ai_task_manager.adapters.hosts.bridge.gateway import SCHEMAS, tool_schema
from ai_task_manager.adapters.hosts.bridge.policy import (
    NativePolicy,
    clean_environment,
    codex_command,
    policy_digest,
)
from ai_task_manager.adapters.hosts.transports.claude import sdk_command, sdk_options
from ai_task_manager.domains.work.errors import AccessDenied


def policy(tmp_path, host):
    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".claude_roni"
    profile.mkdir()
    isolated = tmp_path / "codex"
    isolated.mkdir(mode=0o700)
    env = (
        clean_environment(home, claude_profile=profile)
        if host == "claude"
        else clean_environment(home, codex_home=isolated)
    )
    binary = Path(sys.executable).resolve()
    names = tuple("mcp__aitm__" + name for name in SCHEMAS)
    model = "fixture-requested-model" if host == "claude" else None
    executable_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    return NativePolicy(
        host,
        binary,
        executable_hash,
        env,
        tmp_path,
        policy_digest(host, executable_hash, env, tmp_path, {}, model=model),
        (),
        names,
        model,
    )


def test_environment_derives_user_and_excludes_all_provider_secrets(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setenv("USER", "caller-impostor")
    for name in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_BASE_URL",
    ]:
        monkeypatch.setenv(name, "PRIVATE")
    p = policy(tmp_path, "claude")
    assert p.environment["USER"] == pwd.getpwuid(os.getuid()).pw_name
    assert "PRIVATE" not in str(p.environment) and "caller-impostor" not in str(p.environment)
    assert Path(p.environment["CLAUDE_CONFIG_DIR"]).name == ".claude_roni"


def test_codex_exact_parser_supported_native_restrictions_are_in_argv(tmp_path):
    p = policy(tmp_path, "codex")
    command = codex_command(p, 30)
    for value in [
        "tools.update_plan.enabled=false",
        "features.shell_tool=false",
        "agents.enabled=false",
        "features.multi_agent=false",
        "features.apps=false",
        'web_search="disabled"',
    ]:
        assert value in command.argv
    assert "--strict-config" in command.argv and "--stdio" in command.argv
    assert "--dangerously-bypass-hook-trust" not in command.argv


def test_claude_uses_custom_owned_transport_and_never_sdk_spawn(tmp_path, monkeypatch):
    p = policy(tmp_path, "claude")
    gateway = SimpleNamespace(
        schemas={name: tool_schema(name) for name in SCHEMAS}, call=lambda *_: None
    )
    options = sdk_options(p, gateway)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Default SDK spawning is forbidden")

    monkeypatch.setattr(SubprocessCLITransport, "connect", forbidden)
    command = sdk_command(p, options, 30)
    assert command.argv[0] == str(p.executable)
    assert "--restricted" in command.argv and "--strict-mcp-config" in command.argv
    assert command.argv[command.argv.index("--tools") + 1] == ""
    assert "--setting-sources=" in command.argv
    assert "--bare" not in command.argv
    assert command.argv[command.argv.index("--max-turns") + 1] == "4"
    assert command.argv[command.argv.index("--max-budget-usd") + 1] == "0.25"
    options.tools = ["Bash"]
    with pytest.raises(AccessDenied):
        sdk_command(p, options, 30)


@pytest.mark.parametrize(
    "field,value", [("max_turns", None), ("max_budget_usd", 100), ("max_buffer_size", None)]
)
def test_claude_finite_limits_cannot_be_changed_after_review(tmp_path, field, value):
    p = policy(tmp_path, "claude")
    gateway = SimpleNamespace(schemas={}, call=lambda *_: None)
    options = sdk_options(p, gateway)
    setattr(options, field, value)
    with pytest.raises(AccessDenied):
        sdk_command(p, options, 30)


def test_wrong_executable_digest_and_extra_environment_refuse(tmp_path):
    p = policy(tmp_path, "codex")
    with pytest.raises(AccessDenied):
        replace(p, executable_sha256="0" * 64).check()
    with pytest.raises(AccessDenied):
        replace(p, environment={**p.environment, "OPENAI_API_KEY": "no"}).check()
