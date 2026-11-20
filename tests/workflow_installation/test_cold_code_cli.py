"""Literal installed cold recovery of retained code; authored provider/native ports."""

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


@pytest.mark.parametrize("crash_stage", ["checks", "worker-commit", "declared-worker-commit"])
def test_installed_original_key_recovers_code_after_actual_owner_crash(tmp_path, crash_stage):
    root = tmp_path.resolve()
    root.chmod(0o700)
    sockets = Path(tempfile.mkdtemp(prefix="t04-code-")).resolve()
    package = Path(os.environ["WORKFLOW_INSTALL_PACKAGE"])
    repository = Path(__file__).parents[2]
    control = root / "control"
    control.mkdir(mode=0o700)
    (control / "sitecustomize.py").write_text(
        "from tests.workflow_installation.cold_code_controls import install;install()\n"
    )
    env = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "T04_COLD_ROOT": str(root),
        "T04_COLD_STAGE": "worker-commit" if crash_stage == "declared-worker-commit" else crash_stage,
        "PYTHONPATH": os.pathsep.join(
            map(
                str,
                (
                    control,
                    package,
                    repository,
                    Path(pytest.__file__).parent.parent,
                ),
            )
        ),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if crash_stage == "declared-worker-commit":
        env["T04_DECLARED_COLD"] = "1"
    child = None
    successor = None
    with (root / "owner.log").open("wb") as output:
        try:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from tests.workflow_installation.normal_code_owner "
                    "import owner_main;import sys;owner_main(*sys.argv[1:])",
                    str(root),
                    str(sockets),
                ],
                env=env,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            end = time.monotonic() + 20
            while (
                not (root / "ready.json").exists()
                and child.poll() is None
                and time.monotonic() < end
            ):
                time.sleep(0.03)
            assert (root / "ready.json").exists(), (root / "owner.log").read_text()
            ready = json.loads((root / "prepared.json").read_text())
            cli = Path(os.environ["AITM_OPERATOR_TEST_VENV"]) / "bin/aitm"

            def call(action, data, installed=False):
                argv = [str(cli), "--config", str(root / "service.json"), "--json"]
                if installed:
                    argv += ["--installation", ready["installation"]]
                argv += ["workflow", action, "--input", "-"]
                result = subprocess.run(
                    argv,
                    input=json.dumps(data),
                    text=True,
                    capture_output=True,
                    timeout=180,
                    env=env,
                )
                (root / (action + "-" + str(time.time_ns()) + ".json")).write_text(
                    json.dumps(
                        {
                            "argv": argv,
                            "input": data,
                            "exit": result.returncode,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                        }
                    )
                )
                return result, json.loads(result.stdout or result.stderr)

            request = {
                "task_id": ready["task_id"],
                "plan_ref": ready["plan_ref"],
                "operation_key": "original-cold-code",
            }
            first, result = call("run", request)
            assert (root / "crashed.json").exists(), result
            child.wait(timeout=8)
            assert child.returncode == 71, (root / "owner.log").read_text()
            if crash_stage == "declared-worker-commit":
                with sqlite3.connect(root / "declared-provider/core.sqlite") as db:
                    snapshot = json.loads(db.execute(
                        "SELECT data FROM scheduling_snapshots WHERE task_id=?",
                        (ready["task_id"],),
                    ).fetchone()[0])
                time.sleep(max(0, snapshot["expires_at"] - time.time() + 0.05))
            state = Path(ready["state_root"])
            with sqlite3.connect(state / "workflow.sqlite") as db:
                reference = db.execute("SELECT id FROM runs").fetchone()[0]
            config = (root / "service.json").read_bytes()
            spec = Path(ready["installation"]).read_bytes()
            from ai_task_manager.adapters.workflow_control.lineage import resolve_control

            original = resolve_control(root / "service.json")
            record = Path(original["record"]).read_bytes()
            resources = {
                str(p): p.stat().st_ino for p in (state / "resources/databases").glob("*.sqlite")
            }
            calls = {}
            for role in ("author", "code_worker", "code_candidate"):
                with sqlite3.connect(root / role / "permission.sqlite") as db:
                    calls[role] = db.execute(
                        "SELECT call_id,status,process_stopped FROM native_calls"
                    ).fetchall()
            observed, recovered = call(
                "recover",
                {
                    "action": "resume",
                    "workflow_ref": reference,
                    "operation_key": request["operation_key"],
                },
                installed=True,
            )
            assert observed.returncode == 0 and recovered["data"]["state"] == "done", recovered
            successor = resolve_control(root / "service.json")
            assert json.loads(Path(successor["record"]).read_text())["pid"] != child.pid
            assert (root / "service.json").read_bytes() == config
            assert Path(ready["installation"]).read_bytes() == spec
            assert Path(original["record"]).read_bytes() == record
            replay, same = call("run", request)
            assert replay.returncode == 0 and same["data"] == recovered["data"]
            assert {
                str(p): p.stat().st_ino for p in (state / "resources/databases").glob("*.sqlite")
            } == resources
            for role in calls:
                with sqlite3.connect(root / role / "permission.sqlite") as db:
                    after = db.execute(
                        "SELECT call_id,status,process_stopped FROM native_calls"
                    ).fetchall()
                assert all(row[2] == 1 for row in after)
                if crash_stage == "checks" or role == "author":
                    assert after == calls[role]
                else:
                    assert calls[role] == [] and after
            assert (
                subprocess.check_output(
                    ["git", "-C", ready["repository"], "show", "integration:src/a.txt"], text=True
                ).strip()
                == "4"
            )
            assert recovered["data"]["cleanup"] == "pending_independent_retirement"
            if crash_stage == "declared-worker-commit":
                with sqlite3.connect(root / "declared-provider/core.sqlite") as db:
                    invalidation = db.execute(
                        "SELECT invalidated_by FROM scheduling_attempts WHERE task_id=?",
                        (ready["task_id"],),
                    ).fetchone()[0]
                    checkpoint = db.execute(
                        "SELECT payload FROM checkpoints WHERE kind='cold_delivery'"
                    ).fetchall()
                assert invalidation and checkpoint
                (root / "dependent-cold-result.json").write_text(json.dumps({
                    "recovered": recovered, "prior_invalidation_retained": invalidation,
                    "cold_checkpoints": [json.loads(row[0]) for row in checkpoint],
                    "age_seconds": time.time() - snapshot["observed_at"],
                    "author_calls_unchanged": True,
                    "publication_witness": "Authored A fact with current5s window; real Core/source checks",
                }, indent=2) + "\n")
        finally:
            (root / "stop-cold-owner").touch()
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=22)
            if successor is not None:
                end = time.monotonic() + 40
                while Path(successor["endpoint"]).exists() and time.monotonic() < end:
                    time.sleep(0.05)
                assert not Path(successor["endpoint"]).exists()
            shutil.rmtree(sockets)
