"""Installed public code run/resume; authored hosts/provider, actual Git/DB/container effects."""

import json
import sqlite3
import subprocess

import pytest

from tests.workflow_installation.test_installation import cli, installed  # noqa: F401, F811


@pytest.mark.parametrize("installed", ["code", "code-restart"], indirect=True)
def test_installed_code_plan_delivers_and_replays(installed):  # noqa: F811
    root, package, child = installed
    ready = json.loads((root / "prepared.json").read_text())
    assert ready["module_source"].startswith(str(package))
    request = {
        "task_id": ready["task_id"],
        "plan_ref": ready["plan_ref"],
        "operation_key": "installed-code-complete",
    }
    wrong = cli(installed, "workflow", "run", dict(request, plan_ref="foreign-plan"))
    assert wrong.returncode == 3, wrong.stdout
    extra = cli(installed, "workflow", "run", dict(request, target="untrusted"))
    assert extra.returncode == 2, extra.stdout
    first = cli(installed, "workflow", "run", request)
    result = json.loads(first.stdout or first.stderr)
    if (root / "lost-reply.json").exists():
        assert first.returncode == 4 and result["data"]["reason"] == "RuntimeError", result
        first = cli(installed, "workflow", "resume", {"workflow_ref": result["data"]["workflow"]})
        result = json.loads(first.stdout or first.stderr)
    assert first.returncode == 0 and result["data"]["state"] == "done", result
    assert result["data"]["cleanup"] == "pending_independent_retirement"
    reference = result["data"]["workflow"]
    replay = cli(installed, "workflow", "run", request)
    assert replay.returncode == 0 and json.loads(replay.stdout)["data"] == result["data"]
    resumed = cli(installed, "workflow", "resume", {"workflow_ref": reference})
    assert resumed.returncode == 0 and json.loads(resumed.stdout)["data"] == result["data"]
    conflict = cli(installed, "workflow", "run", dict(request, operation_key="another-key"))
    assert conflict.returncode == 2
    with sqlite3.connect(root / "installed-state/workflow.sqlite") as db:
        rows = {
            name: (json.loads(payload), json.loads(value) if value else None)
            for name, payload, value in db.execute("SELECT name,payload,result FROM steps")
        }
        assert rows["worker-commit"][1]["state"] == "committed"
        assert rows["unit:complete"][1] is not None
        assert (
            rows["code-plan"][0]["plan"]["grant"]["task_version"]
            > rows["worker-commit"][0]["task_version"]
        )
        assert rows["code-merge"][1]["state"] == "done"
    with sqlite3.connect(root / "core.sqlite") as db:
        body = json.loads(
            db.execute("SELECT data FROM tasks WHERE id=?", (ready["task_id"],)).fetchone()[0]
        )
        assert body["observed_status"] == "In Dev" and body["delivered_once"]
    target = subprocess.check_output(
        ["git", "-C", str(root / "source"), "show", "integration:src/a.txt"], text=True
    ).strip()
    assert target == "4"
    assert list((root / "installed-state/resources/databases").glob("*.sqlite"))
    import os
    import sys

    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-I",
            "-c",
            "import sys;sys.path.insert(0,sys.argv.pop(1));"
            "from ai_task_manager.presentation.cli.main import main;raise SystemExit(main())",
            str(package),
            "--config",
            str(root / "service.json"),
            "mcp",
            "serve",
        ],
        env={"PATH": os.defpath},
        cwd=root,
    )

    async def mcp_resume():
        async with Client(params, read_timeout_seconds=20) as client:
            response = await client.call_tool("workflow_resume", {"workflow_ref": reference})
            assert response.structured_content["data"] == result["data"]

    anyio.run(mcp_resume)
    assert child.poll() is None


@pytest.mark.parametrize(
    "installed", ["code-missing-registration", "code-wrong-policy"], indirect=True
)
def test_installed_code_policy_missing_or_mismatched_refuses_before_worker(installed):  # noqa: F811
    root, _, _ = installed
    refused = json.loads((root / "refused.json").read_text())
    assert refused["type"] in {"Unavailable", "AccessDenied"}
    with sqlite3.connect(root / "core.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
