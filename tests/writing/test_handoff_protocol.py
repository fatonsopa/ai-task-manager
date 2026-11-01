"""Installed CLI and actual MCP stdio; authored reviews, no live provider/model."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ai_task_manager.application.service.models import SCHEMAS
from ai_task_manager.config.files import read_private, write_private
from tests.application.helpers import FEATURE_BODY, ServiceRig, reviewed_control


@pytest.fixture(scope="module")
def installed(tmp_path_factory, record_testsuite_property):
    root = Path(__file__).resolve().parents[2]
    destination = tmp_path_factory.mktemp("writer-installed")
    commands = [
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(destination / "wheels")],
        ["uv", "venv", "--python", sys.executable, str(destination / "env")],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
    python = destination / "env/bin/python"
    wheel = next((destination / "wheels").glob("*.whl"))
    result = subprocess.run(
        ["uv", "pip", "install", "--offline", "--python", str(python), str(wheel)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    checked = subprocess.run(
        [str(python), "-I", "-c", "import ai_task_manager; print(ai_task_manager.__file__)"],
        cwd=destination,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr
    assert str(destination / "env") in checked.stdout and str(root / "src") not in checked.stdout
    record_testsuite_property(
        "writer_handoff_wheel_sha256", hashlib.sha256(wheel.read_bytes()).hexdigest()
    )
    record_testsuite_property("writer_handoff_installed_module", checked.stdout.strip())
    return destination / "env/bin/aitm", environment


@pytest.fixture
def configured(tmp_path):
    source = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    if not source:
        pytest.skip("Configure the actual approved Writer package for integration checks")
    env = ServiceRig(tmp_path / "state", writer_root=source)
    config = env.configuration()
    values = read_private(config)
    values["writer"] = {
        "root": str(env.writer.package.root),
        "python": sys.executable,
        "pins": env.writer.package.pins,
        "sessions_root": str(env.writer.root),
        "journal": str(env.writer.journal.path),
        "key": str(env.writer.journal.key_path),
    }
    write_private(config, values, replace=True)
    return env, config


def prepare_input():
    return {
        "kind": "issue_feature",
        "purpose": "task",
        "title": "Verify scoped task ownership",
        "body": FEATURE_BODY,
        "code_bearing": True,
        "required_work": ["complete"],
    }


def assert_original(env, prepared, payload):
    assert set(payload) == {"approval_ref", "purpose", "kind", "policy", "digest", "handoff"}
    folder, _ = env.writer._session(prepared["writing_id"], env.rig.scope)
    receipt = read_private(folder / "validator/delivery.json")
    assert payload["handoff"] == {
        "status": "delivered",
        **{key: value for key, value in receipt.items() if key != "review"},
    }
    assert payload["handoff"]["message"].encode() == (folder / "validator/message.md").read_bytes()
    assert env.rig.core.list_tasks(env.rig.handle) == []
    assert env.rig.core.sync.list_operations(env.rig.handle) == []
    assert env.rig.core.notifications.list_events(env.rig.handle) == []
    assert not env.provider.calls


def test_wh02_installed_cli_preserves_actual_handoff_and_scope(configured, installed, tmp_path):
    env, config = configured
    cli, environment = installed

    def call(action, data, expected=0):
        result = subprocess.run(
            [str(cli), "--config", str(config), "--json", "writing", action, "--input", "-"],
            cwd=tmp_path,
            env=environment,
            input=json.dumps(data),
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == expected, result.stderr
        return json.loads(result.stdout if expected == 0 else result.stderr)

    prepared = call("prepare", prepare_input())["data"]
    missing_review = call("finalize", {"writing_id": prepared["writing_id"]}, expected=3)
    assert not missing_review["ok"] and missing_review["outcome"] == "denied"
    reviewed = call(
        "review",
        {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
    )
    assert reviewed["data"]["accepted"]
    finalized = call("finalize", {"writing_id": prepared["writing_id"]})
    assert_original(env, prepared, finalized["data"])
    folder, _ = env.writer._session(prepared["writing_id"], env.rig.scope)
    raw = (folder / "validator/message.md").read_bytes()
    (folder / "validator/message.md").write_bytes(raw.rstrip(b"\n"))
    # Use the approved writing ref through the installed existing task command.
    refused = subprocess.run(
        [str(cli), "--config", str(config), "--json", "task", "submit", "--input", "-"],
        cwd=tmp_path,
        env=environment,
        input=json.dumps(
            {"writing_ref": finalized["data"]["approval_ref"], "operation_key": "stale"}
        ),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert refused.returncode == 3 and json.loads(refused.stderr)["outcome"] == "denied"
    assert not env.rig.core.list_tasks(env.rig.handle)


def test_wh03_installed_actual_sdk_stdio_returns_original_handoff(configured, installed, tmp_path):
    env, config = configured
    cli, environment = installed
    params = StdioServerParameters(
        command=str(cli),
        args=["--config", str(config), "mcp", "serve"],
        env=environment,
        cwd=tmp_path,
    )

    async def check():
        async with Client(params, read_timeout_seconds=30) as client:
            assert client.server_info.name == "ai-task-manager" and client.protocol_version
            listing = await client.list_tools()
            assert {tool.name: tool.input_schema for tool in listing.tools} == SCHEMAS
            response = await client.call_tool("writing_prepare", prepare_input())
            assert not response.is_error, response
            prepared = response.structured_content["data"]
            denied = await client.call_tool(
                "writing_finalize", {"writing_id": "writing-session:unknown"}
            )
            assert denied.is_error and denied.structured_content["outcome"] == "denied"
            response = await client.call_tool(
                "writing_review",
                {
                    "writing_id": prepared["writing_id"],
                    "review": reviewed_control(prepared["packet"]),
                },
            )
            assert not response.is_error and response.structured_content["data"]["accepted"]
            finalized = await client.call_tool(
                "writing_finalize", {"writing_id": prepared["writing_id"]}
            )
            assert not finalized.is_error, finalized
            text = json.loads(finalized.content[0].text)
            assert text == finalized.structured_content
            assert_original(env, prepared, text["data"])
            ref = text["data"]["approval_ref"]
            folder, _ = env.writer._session(prepared["writing_id"], env.rig.scope)
            (folder / "accepted.md").write_bytes(b"Changed delivered content.\n")
            refused = await client.call_tool(
                "task_submit", {"writing_ref": ref, "operation_key": "changed"}
            )
            assert refused.is_error and refused.structured_content["outcome"] == "denied"
            assert not env.rig.core.list_tasks(env.rig.handle)

    anyio.run(check)
