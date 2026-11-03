"""Public installed clients reach one real retained owner without effect replay."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ai_task_manager.config.files import read_private


@pytest.fixture
def owned(tmp_path, request):
    root = tmp_path.resolve()
    root.chmod(0o700)
    socket_root = Path(tempfile.mkdtemp(prefix="aitm-owner-")).resolve()
    package = Path(os.environ["WORKFLOW_INSTALLED_PACKAGE"])
    repository = Path(__file__).resolve().parents[2]
    args = [
        sys.executable,
        "-I",
        "-c",
        "import sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];"
        "from tests.workflow_control.helpers import entry;entry()",
        str(package),
        str(repository),
        str(root),
        str(socket_root),
        *getattr(request, "param", ()),
    ]
    out = (root / "owner.stdout").open("wb")
    err = (root / "owner.stderr").open("wb")
    child = subprocess.Popen(
        args,
        stdout=out,
        stderr=err,
        start_new_session=True,
        env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    deadline = time.monotonic() + 12
    while (
        not (root / "ready.json").exists() and child.poll() is None and time.monotonic() < deadline
    ):
        time.sleep(0.03)
    if not (root / "ready.json").exists():
        child.terminate()
        child.wait(timeout=8)
        pytest.fail((root / "owner.stderr").read_text())
    ready = read_private(root / "ready.json")
    assert ready["module_source"].startswith(str(package))
    value = {
        "root": root,
        "socket": socket_root,
        "child": child,
        "ready": ready,
        "package": package,
    }
    try:
        yield value
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
        out.close()
        err.close()
        if "crash" in getattr(request, "param", ()):
            assert child.returncode == -9
            import sqlite3

            with sqlite3.connect(root / "runtime.sqlite") as conn:
                facts = [
                    json.loads(r[0])
                    for r in conn.execute(
                        "SELECT data FROM runtime_observations WHERE kind='process_created'"
                    )
                ]
            ids = {child.pid} | {fact["pid"] for fact in facts}
            limit = time.monotonic() + 5
            while True:
                inventory = subprocess.check_output(
                    ["ps", "-axo", "pid=,pgid=,stat=,lstart="], text=True
                )
                remaining = [
                    r
                    for r in inventory.splitlines()
                    if len(r.split()) >= 3
                    and int(r.split()[1]) in ids
                    and not r.split()[2].startswith(("Z", "X"))
                ]
                if not remaining or time.monotonic() >= limit:
                    break
                time.sleep(0.02)
            assert not remaining
            stopped = {
                "owner_pid": child.pid,
                "owner_closed": False,
                "crashed": True,
                "children_observed_absent_after_EOF": True,
                "canonical_recovery_obligations": "not settled by this observation",
                "workers": [],
            }
            report = os.environ.get("WORKFLOW_ENTRYPOINT_REPORT")
            if report:
                with Path(report).open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "owner": stopped,
                                "original_children": facts,
                                "remaining": remaining,
                                "root": str(root),
                            }
                        )
                        + "\n"
                    )
            shutil.rmtree(socket_root)
            return
        assert child.returncode == 0, (root / "owner.stderr").read_text()
        stopped = read_private(root / "stop.json")
        assert stopped["owner_closed"]
        inventory = subprocess.check_output(["ps", "-axo", "pid=,pgid=,stat=,lstart="], text=True)
        ids = {child.pid} | {w["group"] for w in stopped["workers"]}
        remaining = [
            r
            for r in inventory.splitlines()
            if len(r.split()) >= 3
            and int(r.split()[1]) in ids
            and not r.split()[2].startswith(("Z", "X"))
        ]
        assert not remaining
        for worker in stopped["workers"]:
            assert worker["returncode"] is not None
            assert worker["outcomes"] and worker["outcomes"][-1]["quiescent"]
        report = os.environ.get("WORKFLOW_ENTRYPOINT_REPORT")
        if report:
            with Path(report).open("a") as stream:
                stream.write(
                    json.dumps({"owner": stopped, "remaining": remaining, "root": str(root)}) + "\n"
                )
        # Retain evidence, but remove only this invocation's already-closed socket directory.
        shutil.rmtree(socket_root)


def cli(owned, action, data, *, config=None, group="workflow"):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys;sys.path.insert(0,sys.argv.pop(1));"
            "from ai_task_manager.presentation.cli.main import main;raise SystemExit(main())",
            str(owned["package"]),
            "--config",
            str(config or owned["ready"]["config"]),
            "--json",
            group,
            action,
            "--input",
            "-",
        ],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        timeout=20,
        env={"PATH": os.defpath},
    )


def run_data(owned):
    return {
        "task_id": owned["ready"]["task_id"],
        "plan_ref": "non-code",
        "operation_key": "public-run",
    }


def parsed(process):
    return json.loads(process.stdout or process.stderr)


def test_WE01_installed_run_and_separate_resume(owned):
    first = cli(owned, "run", run_data(owned))
    body = parsed(first)
    assert first.returncode == 4 and body["data"]["state"] == "awaiting_review", body
    ref = body["data"]["workflow"]
    again = cli(owned, "resume", {"workflow_ref": ref})
    assert again.returncode == 4 and parsed(again)["data"] == body["data"]
    import sqlite3

    with sqlite3.connect(owned["root"] / "runtime.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1
    with sqlite3.connect(owned["root"] / "workflow.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 3


@pytest.mark.parametrize("owned", [("drop",)], indirect=True)
def test_WE08_lost_initial_reply_reuses_same_workflow(owned):
    first = cli(owned, "run", run_data(owned))
    assert first.returncode == 6, parsed(first)
    second = cli(owned, "run", run_data(owned))
    assert second.returncode == 4, parsed(second)
    import sqlite3

    with sqlite3.connect(owned["root"] / "runtime.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1


def test_WE05_copied_configuration_cannot_use_owner(owned):
    config = Path(owned["ready"]["config"])
    copy = owned["root"] / "copied.json"
    shutil.copyfile(config, copy)
    copy.chmod(0o600)
    result = cli(owned, "run", run_data(owned), config=copy)
    assert result.returncode == 3, parsed(result)
    import sqlite3

    with sqlite3.connect(owned["root"] / "runtime.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", True),
        ("command", ["echo", "x"]),
        ("plan_ref", "missing"),
        ("task_id", "foreign-task"),
    ],
)
def test_WE04_unknown_or_model_selected_authority_refuses(owned, field, value):
    data = run_data(owned)
    data[field] = value
    result = cli(owned, "run", data)
    assert result.returncode in {2, 3}, parsed(result)


def test_WE07_concurrent_clients_share_one_owner(owned):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: cli(owned, "run", run_data(owned)), range(2)))
    assert all(r.returncode == 4 for r in replies), [parsed(r) for r in replies]
    assert len({parsed(r)["data"]["workflow"] for r in replies}) == 1


def test_WE06_stale_owner_refuses_without_replacement(owned):
    owned["child"].terminate()
    owned["child"].wait(timeout=10)
    result = cli(owned, "run", run_data(owned))
    assert result.returncode in {3, 5, 6}, parsed(result)
    assert not (owned["socket"] / "owner.sock").exists()


def test_WE12_unsigned_invalid_request_does_not_stop_owner(owned):
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(2)
        stream.connect(str(owned["socket"] / "owner.sock"))
        stream.sendall(b'{"operation":"workflow_run"}\n')
        assert stream.recv(1) == b""
    result = cli(owned, "run", run_data(owned))
    assert result.returncode == 4, parsed(result)


@pytest.mark.parametrize("owned", [("step", "crash")], indirect=True)
def test_WE11_stopped_owner_does_not_invent_resumed_handles(owned):
    first = cli(owned, "run", run_data(owned))
    body = parsed(first)
    assert first.returncode == 4 and body["data"]["state"] == "checkpoint", body
    owned["child"].kill()
    owned["child"].wait(timeout=10)
    again = cli(owned, "resume", {"workflow_ref": body["data"]["workflow"]})
    assert again.returncode == 3, parsed(again)
    import sqlite3

    with sqlite3.connect(owned["root"] / "runtime.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1
    with sqlite3.connect(owned["root"] / "core.sqlite") as conn:
        rows = conn.execute("SELECT data FROM attempts").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["state"] == "running"


def test_WE05_foreign_principal_descriptor_refuses(owned):
    from ai_task_manager.config.files import write_private

    record = owned["socket"] / "owner.json"
    descriptor = read_private(record)
    descriptor["scope"]["principal"] = "another-principal"
    write_private(record, descriptor, replace=True)
    response = cli(owned, "run", run_data(owned))
    assert response.returncode == 3, parsed(response)


def test_WE02_actual_mcp_uses_same_owner_and_schema(owned):
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    from ai_task_manager.application.service.models import SCHEMAS

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-I",
            "-c",
            "import sys;sys.path.insert(0,sys.argv.pop(1));"
            "from ai_task_manager.presentation.cli.main import main;raise SystemExit(main())",
            str(owned["package"]),
            "--config",
            owned["ready"]["config"],
            "mcp",
            "serve",
        ],
        env={"PATH": os.defpath},
        cwd=owned["root"],
    )
    seen = []

    async def check():
        async with Client(params, read_timeout_seconds=20) as client:
            listed = await client.list_tools()
            assert {t.name: t.input_schema for t in listed.tools} == SCHEMAS
            first = await client.call_tool("workflow_run", run_data(owned))
            assert first.structured_content["data"]["state"] == "awaiting_review"
            reference = first.structured_content["data"]["workflow"]
            again = await client.call_tool("workflow_resume", {"workflow_ref": reference})
            assert again.structured_content["data"] == first.structured_content["data"]
            inventory = subprocess.check_output(
                ["ps", "-axo", "pid=,ppid=,lstart=,command="], text=True
            )
            seen.extend(
                int(line.split()[0])
                for line in inventory.splitlines()
                if owned["ready"]["config"] in line and "mcp serve" in line
            )
            assert seen

    anyio.run(check)
    after = subprocess.check_output(["ps", "-axo", "pid=,stat="], text=True)
    live = {
        int(r.split()[0])
        for r in after.splitlines()
        if len(r.split()) > 1 and not r.split()[1].startswith(("Z", "X"))
    }
    assert not set(seen) & live
    report = os.environ.get("WORKFLOW_ENTRYPOINT_REPORT")
    if report:
        with Path(report).open("a") as stream:
            stream.write(
                json.dumps({"mcp_pids": seen, "remaining_mcp": list(set(seen) & live)}) + "\n"
            )


@pytest.mark.parametrize("changed", ["task_id", "operation_key"])
def test_WE09_original_task_and_key_binding(owned, changed):
    first = cli(owned, "run", run_data(owned))
    assert first.returncode == 4, parsed(first)
    data = run_data(owned)
    data[changed] = owned["ready"]["other_task_id"] if changed == "task_id" else "different-key"
    second = cli(owned, "run", data)
    assert second.returncode == 2, parsed(second)
    import sqlite3

    with sqlite3.connect(owned["root"] / "runtime.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1


def test_WE03_installed_prior_command_preserved(owned):
    response = cli(owned, "list", {}, group="task")
    assert response.returncode == 0, parsed(response)
    body = parsed(response)
    assert owned["ready"]["task_id"] in {t["id"] for t in body["data"]}
    assert not any(
        word in response.stdout for word in ["capability", "owner.json", "mac", "secret"]
    )


@pytest.mark.parametrize("owned", [("barrier",)], indirect=True)
def test_WE14_different_task_read_and_human_hold_respond_during_execution(owned):
    from concurrent.futures import ThreadPoolExecutor

    started = time.monotonic()
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(cli, owned, "run", run_data(owned))
        marker = owned["root"] / "barrier-entered"
        limit = time.monotonic() + 6
        while not marker.exists() and not first.done() and time.monotonic() < limit:
            time.sleep(0.02)
        assert marker.exists(), parsed(first.result()) if first.done() else "barrier not observed"
        try:
            read = cli(owned, "show", {"task_id": owned["ready"]["task_id"]}, group="task")
            assert read.returncode == 0, parsed(read)
            before_other = time.monotonic()
            other = cli(
                owned,
                "run",
                {
                    "task_id": owned["ready"]["other_task_id"],
                    "plan_ref": "other",
                    "operation_key": "other-task",
                },
            )
            other_duration = time.monotonic() - before_other
            assert other.returncode == 4 and parsed(other)["data"]["state"] == "awaiting_review", (
                parsed(other)
            )
            assert other_duration < 6 and not first.done()
            current = parsed(read)["data"]
            hold = cli(
                owned,
                "action",
                {
                    "task_id": current["id"],
                    "action": "hold",
                    "expected_version": current["version"],
                    "decision_revision": current["decision_revision"],
                    "operation_key": "human-hold",
                    "human_ref": "fixture-hold",
                },
                group="task",
            )
            # Hold intent is authoritative immediately; legal On Hold still needs
            # actual quiescence and may be refused while the worker is stopping.
            assert hold.returncode in {0, 2, 3, 4, 6}, parsed(hold)
            shown = cli(owned, "show", {"task_id": current["id"]}, group="task")
            assert shown.returncode == 0 and parsed(shown)["data"]["human_hold"] is True, parsed(
                shown
            )
            ended = first.result(timeout=6)
            assert parsed(ended)["data"]["state"] == "blocked", parsed(ended)
            report = os.environ.get("WORKFLOW_ENTRYPOINT_REPORT")
            if report:
                with Path(report).open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "barrier_control": {
                                    "other_seconds": other_duration,
                                    "elapsed_seconds": time.monotonic() - started,
                                    "read_before_release": True,
                                    "hold_observed": True,
                                }
                            }
                        )
                        + "\n"
                    )
        finally:
            (owned["root"] / "barrier-release").write_text("release")


def test_WE12_authenticated_nonce_replay_is_rejected(owned):
    import socket

    from ai_task_manager.adapters.workflow_control.identity import canonical, receive, signature

    row = read_private(owned["socket"] / "owner.json")
    body = {
        "nonce": "a" * 64,
        "operation": "task_list",
        "data": {},
        "scope": row["scope"],
        "instance": row["instance"],
    }
    frame = canonical(dict(body, mac=signature(row["key"], body))) + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        stream.connect(row["endpoint"]["path"])
        stream.sendall(frame)
        response = receive(stream)
        assert response["envelope"]["ok"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        stream.connect(row["endpoint"]["path"])
        stream.sendall(frame)
        assert stream.recv(1) == b""
    assert cli(owned, "list", {}, group="task").returncode == 0


def test_WE12_tampered_response_cannot_claim_done(owned, monkeypatch):
    from ai_task_manager.adapters.workflow_control.client import WorkflowClient
    from ai_task_manager.domains.work.errors import AccessDenied

    row = read_private(owned["socket"] / "owner.json")
    monkeypatch.setattr(
        "ai_task_manager.adapters.workflow_control.client.receive",
        lambda stream: {
            "nonce": "wrong",
            "mac": "0" * 64,
            "envelope": {"ok": True, "data": {"state": "done"}},
        },
    )
    with pytest.raises(AccessDenied):
        WorkflowClient(owned["ready"]["config"], row["scope"]).forward("task_list", {})


def test_WE12_oversize_frame_does_not_disable_owner(owned):
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        stream.connect(str(owned["socket"] / "owner.sock"))
        try:
            stream.sendall(b"x" * 262146 + b"\n")
            assert stream.recv(1) == b""
        except (BrokenPipeError, ConnectionResetError):
            pass
    assert cli(owned, "list", {}, group="task").returncode == 0


@pytest.mark.parametrize("field", ["pid", "source", "endpoint"])
def test_WE06_forged_owner_identity_is_refused(owned, field):
    from ai_task_manager.config.files import write_private

    record = owned["socket"] / "owner.json"
    row = read_private(record)
    if field == "pid":
        row["pid"] = -1
    elif field == "source":
        row["source"] = "0" * 64
    else:
        row["endpoint"]["inode"] += 1
    write_private(record, row, replace=True)
    result = cli(owned, "run", run_data(owned))
    assert result.returncode == 3, parsed(result)
