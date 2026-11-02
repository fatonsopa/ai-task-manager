import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import anyio
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from ai_task_manager.application.service.models import SCHEMAS
from ai_task_manager.config.files import write_private
from tests.application.helpers import FEATURE_BODY

from .service_support import approve, compose, configuration


@pytest.fixture(scope="module")
def installed():
    root = Path(__file__).parents[2]
    destination = root / ".local/existing-results/installed" / str(uuid4())
    destination.mkdir(parents=True)
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(destination / "dist")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    (destination / "build.log").write_text(build.stdout + build.stderr)
    assert build.returncode == 0, build.stderr
    (wheel,) = (destination / "dist").glob("*.whl")
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(destination / "venv")],
        check=True,
        capture_output=True,
        timeout=60,
    )
    python = destination / "venv/bin/python"
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    (destination / "install.log").write_text(install.stdout + install.stderr)
    assert install.returncode == 0, install.stderr
    aitm = destination / "venv/bin/aitm"
    bootstrap = root / "tests/fixtures/existing/installed_bootstrap.py"
    imported = subprocess.run(
        [str(python), "-c", "import ai_task_manager; print(ai_task_manager.__file__)"],
        cwd=destination,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert "/site-packages/ai_task_manager/" in imported.stdout, imported.stdout
    manifest = {
        "wheel": str(wheel),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "bootstrap_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
        "crash_harness_sha256": hashlib.sha256(
            bootstrap.with_name("crash_after_admit.py").read_bytes()
        ).hexdigest(),
        "entrypoint_sha256": hashlib.sha256(aitm.read_bytes()).hexdigest(),
        "imported_module": imported.stdout.strip(),
        "scope": "Installed authored-transport boundaries",
        "live_provider": "not_run",
        "real_host_selection": "not_run",
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return python, aitm, bootstrap, destination


def configured(rig, provider):
    service = compose(rig)
    issue = provider.seed("Todo")
    provider.issues[issue]["description"] = FEATURE_BODY
    approval = approve(service, provider.issues[issue]["title"], FEATURE_BODY)
    inspected = rig.intake.inspect(issue)
    selection = rig.select(inspected["discovery_ref"])
    path = rig.root / "installed-service.json"
    config = configuration(rig, service, path)
    return service, issue, approval["approval_ref"], selection, path, config


def environment(provider):
    result = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result["AITM_FIXTURE_ENDPOINT"] = provider.endpoint
    return result


def test_ea59_ea60_installed_cli_runs_real_read_admit_and_denial_paths(installed, rig, provider):
    python, aitm, bootstrap, destination = installed
    service, issue, writing, selection, config_path, config = configured(rig, provider)

    def call(action, data):
        process = subprocess.run(
            [
                str(python),
                str(bootstrap),
                str(aitm),
                "--config",
                str(config_path),
                "--json",
                "task",
                action,
                "--input",
                "-",
            ],
            input=json.dumps(data),
            cwd=destination,
            env=environment(provider),
            capture_output=True,
            text=True,
            timeout=30,
        )
        envelope = json.loads(process.stdout if process.returncode == 0 else process.stderr)
        return process, envelope

    process, found = call("find", {"query": "ownership"})
    assert process.returncode == 0 and found["data"]["candidates"][0]["issue_id"] == issue
    _, inspected = call("inspect", {"issue": issue})
    ref = inspected["data"]["discovery_ref"]
    before = rig.effects()
    process, denied = call(
        "admit",
        {
            "discovery_ref": ref,
            "writing_ref": writing,
            "selection_ref": "fake-human-acceptance",
            "operation_key": "installed-denial",
        },
    )
    assert process.returncode == 3 and denied["outcome"] == "denied"
    assert rig.effects() == before
    process, admitted = call(
        "admit",
        {
            "discovery_ref": ref,
            "writing_ref": writing,
            "selection_ref": selection,
            "operation_key": "installed-admission",
        },
    )
    assert process.returncode == 0 and admitted["data"]["issue_id"] == issue, admitted
    assert not admitted["data"]["queued_once"]
    _, shown = call("show", {"task_id": admitted["data"]["id"]})
    assert shown["data"]["title"] == provider.issues[issue]["title"]
    rig.assert_no_provider_mutations()
    assert len(rig.core.list_tasks(rig.handle)) == 1
    plain = dict(config)
    del plain["linear"]
    plain_path = rig.root / "plain-installed.json"
    write_private(plain_path, plain)
    invalid = subprocess.run(
        [str(aitm), "--config", str(plain_path), "--json", "task", "admit", "--input", "-"],
        input=json.dumps({"human": True}),
        capture_output=True,
        text=True,
        cwd=destination,
        env=environment(provider),
        timeout=15,
    )
    assert invalid.returncode == 2 and json.loads(invalid.stderr)["outcome"] == "invalid_request"
    unknown = subprocess.run(
        [str(aitm), "--config", str(plain_path), "--json", "task", "show", "--input", "-"],
        input=json.dumps({"task_id": str(uuid4())}),
        capture_output=True,
        text=True,
        cwd=destination,
        env=environment(provider),
        timeout=15,
    )
    assert unknown.returncode == 3 and json.loads(unknown.stderr)["outcome"] == "denied"


def test_ea61_ea62_actual_sdk_stdio_to_installed_entrypoint(installed, rig, provider):
    python, aitm, bootstrap, destination = installed
    service, issue, writing, selection, config_path, config = configured(rig, provider)
    forged_other_scope = rig.authority._evidence(
        "existing_issue_selection",
        replace(rig.scope, logical_session="other"),
        issue,
        {"discovery_digest": "a" * 64, "claim_store": rig.claim.context},
        "synthetic-wrong-session-selection",
    )
    params = StdioServerParameters(
        command=str(python),
        args=[str(bootstrap), str(aitm), "--config", str(config_path), "mcp", "serve"],
        env=environment(provider),
        cwd=destination,
    )

    async def check():
        async with Client(params, read_timeout_seconds=30) as client:
            listed = await client.list_tools()
            assert {tool.name: tool.input_schema for tool in listed.tools} == SCHEMAS
            found = await client.call_tool("task_find", {"query": "ownership"})
            assert found.structured_content["data"]["candidates"][0]["issue_id"] == issue
            inspected = await client.call_tool("task_inspect", {"issue": issue})
            ref = inspected.structured_content["data"]["discovery_ref"]
            before = rig.effects()
            denied = await client.call_tool(
                "task_admit",
                {
                    "discovery_ref": ref,
                    "writing_ref": writing,
                    "selection_ref": forged_other_scope,
                    "operation_key": "mcp-cross-session",
                },
            )
            assert denied.is_error and denied.structured_content["outcome"] == "denied"
            assert rig.effects() == before
            unknown = await client.call_tool("mint_selection", {"human": True})
            assert unknown.is_error and rig.effects() == before
            admitted = await client.call_tool(
                "task_admit",
                {
                    "discovery_ref": ref,
                    "writing_ref": writing,
                    "selection_ref": selection,
                    "operation_key": "mcp-admission",
                },
            )
            assert not admitted.is_error, admitted
            assert admitted.structured_content["data"]["issue_id"] == issue
            assert not admitted.structured_content["data"]["queued_once"]
            text = json.dumps(admitted.structured_content)
            assert "reviewed_targets" not in text and "authority_key" not in text

    anyio.run(check)
    rig.assert_no_provider_mutations()


def test_ea49_actual_process_crash_then_restart_recovers_committed_admission(
    installed, rig, provider
):
    python, aitm, bootstrap, destination = installed
    service, issue, writing, selection, config_path, config = configured(rig, provider)
    inspected = rig.intake.inspect(issue)
    data = {
        "discovery_ref": inspected["discovery_ref"],
        "writing_ref": writing,
        "selection_ref": selection,
        "operation_key": "crash-restart-admission",
    }
    arguments = [str(aitm), "--config", str(config_path), "--json", "task", "admit", "--input", "-"]
    crashed = subprocess.run(
        [str(python), str(bootstrap.with_name("crash_after_admit.py")), *arguments],
        input=json.dumps(data),
        cwd=destination,
        env=environment(provider),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 86 and not crashed.stdout, crashed.stderr
    tasks = rig.core.list_tasks(rig.handle)
    assert len(tasks) == 1 and tasks[0].issue_id == issue
    before = rig.effects()
    record = service.writer.journal._get(writing, "writing-task:")
    Path(record["handoff"]["destination"]).unlink()
    recovered = subprocess.run(
        [str(python), str(bootstrap), *arguments],
        input=json.dumps(data),
        cwd=destination,
        env=environment(provider),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    assert result["data"]["id"] == tasks[0].id
    assert rig.effects() == before
    assert result["data"]["execution_state"] == "idle" and not result["data"]["queued_once"]
    rig.assert_no_provider_mutations()
