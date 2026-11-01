import json
from dataclasses import replace

import pytest

from ai_task_manager.adapters.hosts import claude, codex
from ai_task_manager.supervisor.accounting import MechanicalCommands
from ai_task_manager.supervisor.models import RuntimeRefused


def encode(events):
    return "\n".join(json.dumps(event) for event in events)


def claude_events():
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-a",
            "model": "observed-fixture-model",
        },
        {
            "type": "assistant",
            "session_id": "session-a",
            "message": {"content": [{"type": "tool_use", "name": "fixture_read"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": "session-a",
            "is_error": False,
            "result": "RUNTIME_SMOKE_OK",
            "usage": {"input_tokens": 2},
            "total_cost_usd": 0.01,
        },
    ]


def codex_events():
    return [
        {"type": "thread.started", "thread_id": "session-a"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "one", "type": "agent_message", "text": "RUNTIME_SMOKE_OK"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 4}},
    ]


@pytest.mark.parametrize("host,events", [(claude, claude_events), (codex, codex_events)])
def test_structured_complete_host_turn_has_distinct_identity_usage_and_result(host, events):
    result = host.parse_turn(
        encode(events()), returncode=0, quiescent=True, expected_session="session-a"
    )
    assert result.session_id == "session-a" and result.final_text == "RUNTIME_SMOKE_OK"
    assert result.usage["input_tokens"] == 2
    # Client estimates do not become actual incremental billing.
    assert not hasattr(result, "actual_cost")


@pytest.mark.parametrize("host,events", [(claude, claude_events), (codex, codex_events)])
@pytest.mark.parametrize(
    "failure",
    [
        "no_init",
        "no_terminal",
        "duplicate_terminal",
        "nonzero",
        "unknown_stop",
        "wrong_identity",
        "bad_json",
        "oversized",
    ],
)
def test_host_failure_or_missing_observations_cannot_be_success(host, events, failure):
    data = events()
    returncode, quiescent, expected = 0, True, "session-a"
    if failure == "no_init":
        data.pop(0)
    elif failure == "no_terminal":
        data.pop()
    elif failure == "duplicate_terminal":
        data.append(data[-1])
    elif failure == "nonzero":
        returncode = 7
    elif failure == "unknown_stop":
        quiescent = False
    elif failure == "wrong_identity":
        expected = "other"
    text = encode(data)
    if failure == "bad_json":
        text += "\n{invalid"
    if failure == "oversized":
        text = json.dumps({"type": "fixture", "data": "x" * (1024 * 1024)})
    with pytest.raises(RuntimeRefused):
        host.parse_turn(text, returncode=returncode, quiescent=quiescent, expected_session=expected)


def test_claude_provider_error_and_cross_session_event_are_refused():
    for change in ("error", "session"):
        data = claude_events()
        if change == "error":
            data[-1]["is_error"] = True
        else:
            data[1]["session_id"] = "other"
        with pytest.raises(RuntimeRefused):
            claude.parse_turn(encode(data), returncode=0, quiescent=True)


def test_codex_failed_turn_and_items_before_start_are_refused():
    for data in (
        [
            {"type": "thread.started", "thread_id": "a"},
            {"type": "turn.failed", "error": {"message": "fixture failure"}},
        ],
        [codex_events()[0], *codex_events()[2:]],
    ):
        with pytest.raises(RuntimeRefused):
            codex.parse_turn(encode(data), returncode=0, quiescent=True)


def test_claude_profile_is_explicit_and_no_bare_or_fallback(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers":{}}')
    options = dict(
        executable="/fixture/claude",
        home=home,
        cwd=tmp_path,
        settings=settings,
        mcp_config=mcp,
        session_id="a",
        prompt="literal; $(must-not-execute)",
    )
    with pytest.raises(RuntimeRefused):
        claude.smoke_command(**options)
    (home / ".claude_roni").mkdir()
    command = claude.smoke_command(**options)
    assert command.environment["CLAUDE_CONFIG_DIR"] == str(home / ".claude_roni")
    assert "--bare" not in command.argv and "--fallback-model" not in command.argv
    assert command.argv[-1] == options["prompt"]
    assert "--restricted" in command.argv and "--strict-mcp-config" in command.argv
    assert command.argv[command.argv.index("--tools") + 1] == ""


def test_codex_isolated_flags_and_explicit_auth_home(tmp_path):
    auth = tmp_path / "auth-home"
    auth.mkdir()
    command = codex.smoke_command(
        executable="/fixture/codex",
        home=tmp_path,
        codex_home=auth,
        cwd=tmp_path,
        prompt="fixed prompt",
    )
    assert command.environment["CODEX_HOME"] == str(auth)
    assert {"--ignore-user-config", "--ephemeral", "--json"}.issubset(command.argv)
    assert command.argv[command.argv.index("--sandbox") + 1] == "read-only"


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_unreviewed_provider_cost_never_gets_launch_authority(rr, host):
    dispatch = rr.start()
    rr.admit(dispatch)
    command = replace(rr.command(dispatch), host=host)
    rr.supervisor.accounting = MechanicalCommands([command])
    with pytest.raises(RuntimeRefused):
        rr.supervisor.execute(rr.rig.handle, dispatch, command, operation_key=rr.rig.key())
    assert rr.journal.effect(dispatch.attempt_id, "work_grant") is None


def test_fixture_authority_refuses_credential_environment(rr):
    dispatch = rr.start()
    command = replace(rr.command(dispatch), environment={"SAMPLE_API_KEY": "fixture-only"})
    authority = MechanicalCommands([command])
    with pytest.raises(RuntimeRefused):
        authority.check(command)
