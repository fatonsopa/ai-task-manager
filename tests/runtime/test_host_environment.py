import os
import pwd
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts import claude, codex


@pytest.mark.parametrize("host", [claude, codex])
def test_host_environment_derives_user_and_keeps_profile_and_api_exclusions(
    tmp_path, monkeypatch, host
):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude_roni").mkdir()
    auth_home = home / ".codex"
    auth_home.mkdir()
    settings, mcp = tmp_path / "settings.json", tmp_path / "mcp.json"
    settings.write_text("{}")
    mcp.write_text('{"mcpServers":{}}')
    monkeypatch.setenv("USER", "untrusted-caller-user")
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.setenv(key, "fixture-value-never-forwarded")
    lookups = []

    def lookup(uid):
        lookups.append(uid)
        return SimpleNamespace(pw_name="trusted-os-user")

    monkeypatch.setattr(pwd, "getpwuid", lookup)
    if host is claude:
        command = host.smoke_command(
            executable="/fixture/claude",
            home=home,
            cwd=tmp_path,
            settings=settings,
            mcp_config=mcp,
            session_id="fixture-session",
            prompt="fixture prompt",
        )
        assert command.environment["CLAUDE_CONFIG_DIR"] == str(home / ".claude_roni")
    else:
        command = host.smoke_command(
            executable="/fixture/codex",
            home=home,
            codex_home=auth_home,
            cwd=tmp_path,
            prompt="fixture prompt",
        )
        assert command.environment["CODEX_HOME"] == str(auth_home)
    assert command.environment["USER"] == "trusted-os-user"
    assert lookups == [os.getuid()]
    assert set(command.environment) <= {"HOME", "PATH", "USER", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}
