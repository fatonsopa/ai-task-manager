"""Default installed join with declared protocols and actual owned Git/containers."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


def cli(fixture, group, action, data=None, extra=()):
    root, package, _ = fixture
    command = [
        sys.executable,
        "-I",
        "-c",
        "import sys;sys.path.insert(0,sys.argv.pop(1));"
        "from ai_task_manager.presentation.cli.main import main;raise SystemExit(main())",
        str(package),
        "--config",
        str(root / "service.json"),
        "--json",
        group,
        action,
        *extra,
    ]
    if data is not None:
        command += ["--input", "-"]
    return subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
        timeout=240,
        env={"PATH": os.defpath},
    )


@pytest.fixture
def installed(tmp_path, request):
    root = tmp_path.resolve()
    root.chmod(0o700)
    sockets = Path(tempfile.mkdtemp(prefix="aitm-normal-code-")).resolve()
    package = Path(os.environ["WORKFLOW_INSTALL_PACKAGE"])
    repo = Path(__file__).parents[2]
    mode = request.param
    command = [
        sys.executable,
        "-I",
        "-c",
        "import sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];"
        "from tests.workflow_installation.normal_code_owner import owner_main;"
        "owner_main(*sys.argv[1:])",
        str(package),
        str(repo),
        str(root),
        str(sockets),
        mode,
    ]
    with (root / "stdout").open("wb") as out, (root / "stderr").open("wb") as err:
        child = subprocess.Popen(
            command,
            stdout=out,
            stderr=err,
            start_new_session=True,
            env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        )
        try:
            end = time.monotonic() + 20
            while (
                not (root / "ready.json").exists()
                and child.poll() is None
                and time.monotonic() < end
            ):
                time.sleep(0.03)
            assert (root / "ready.json").exists(), (root / "stderr").read_text()
            yield root, package, child
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=22)
            shutil.rmtree(sockets)
            record = os.environ.get("WORKFLOW_INSTALL_REPORT")
            if record:
                with Path(record).open("a") as stream:
                    stream.write(
                        json.dumps({"root": str(root), "pid": child.pid, "exit": child.returncode})
                        + "\n"
                    )
            assert child.returncode == 0, (root / "stderr").read_text()


@pytest.mark.parametrize("installed", ["ordinary", "restart"], indirect=True)
def test_normal_default_code_plan_delivers_and_replays(installed):  # noqa: F811
    root, package, child = installed
    ready = json.loads((root / "prepared.json").read_text())
    assert ready["module_source"].startswith(str(package))
    state = Path(ready["state_root"])
    selected = json.loads((root / "default-resolved.json").read_text())
    assert not selected["code_registration_injected"]
    assert not selected["reviewer_factory_injected"]
    assert selected["prepare_owner_module"].endswith("native.code_provisioning")
    assert selected["reviewer_factory_module"].endswith("native.reviewer")
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
    with sqlite3.connect(state / "workflow.sqlite") as db:
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
    assert list((state / "resources/databases").glob("*.sqlite"))
    for role in ("author", "code_worker", "code_candidate"):
        path = root / role / "permission.sqlite"
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            calls = db.execute("SELECT purpose,status,process_stopped FROM native_calls").fetchall()
            assert calls and all(row[2] == 1 for row in calls), (role, calls)
        if role != "author":
            with sqlite3.connect(root / role / "state/runtime.sqlite") as db:
                assert db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1
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
