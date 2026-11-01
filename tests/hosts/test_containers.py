"""Actual lightweight Linux containers; no host/provider/model acceptance claim."""

import hashlib
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.container_plan import ContainerPlan
from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
from ai_task_manager.adapters.hosts.bridge.gateway import ManagedToolGateway
from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.domains.work.errors import AccessDenied
from tests.runtime.helpers import RuntimeRig

IMAGE = "sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f"
DOCKER = pytest.mark.skipif(
    os.environ.get("AITM_RUN_DOCKER") != "1", reason="Explicit scoped container batch not selected"
)


@pytest.fixture
def command_fixture(tmp_path, journal, request):
    if os.environ.get("AITM_RUN_DOCKER") != "1":
        pytest.skip("Scoped container batch not selected")
    executable = shutil.which("docker")
    assert executable, "Installed Docker CLI is required for this selected batch"
    endpoint = subprocess.run(
        [executable, "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}'],
        check=True,
        capture_output=True,
        text=True,
        timeout=4,
    ).stdout.strip()
    evidence = Path(os.environ["AITM_TEST_EVIDENCE_DIR"]) / "containers" / request.node.name
    evidence.mkdir(mode=0o700, parents=True)
    r = RuntimeRig(tmp_path / "runtime")
    dispatch = r.start()
    attempt = r.admit(dispatch)
    gateway = ManagedToolGateway(
        r.rig.core,
        r.rig.handle,
        dispatch,
        Workspace(dispatch.resources.worktree),
        r.resources,
        journal,
        deadline=journal.clock() + 30,
    )
    gateway.admit()

    @contextmanager
    def effect():
        with r.rig.core.execution.work_boundary(r.rig.handle, attempt.id, attempt.version):
            yield

    def boundary():
        return r.rig.core.execution.validate_work_lease(r.rig.handle, attempt.id, attempt.version)

    def create(plans):
        return DockerCommands(
            journal, evidence / "runner", executable, endpoint, plans, deadline=journal.clock() + 20
        )

    try:
        yield r, dispatch, gateway, boundary, effect, create, evidence
    finally:
        r.close()


@DOCKER
def test_owned_edit_runs_actual_isolated_test_and_verifies_native_restrictions(command_fixture):
    r, dispatch, gateway, boundary, effect, create, evidence = command_fixture
    gateway.call(
        "write_file",
        {
            "path": "maths.py",
            "text": "def answer(): return 41\n",
            "expected_hash": None,
            "operation_key": "initial-code",
        },
    )
    tests = (
        "import unittest,sqlite3,os\nfrom maths import answer\n"
        "class Check(unittest.TestCase):\n"
        " def test_answer(self): self.assertEqual(answer(),42)\n"
        " def test_database(self):\n"
        "  db=sqlite3.connect(os.environ['TASK_DATABASE'])\n"
        "  self.assertEqual(len(db.execute('select attempt_id from fixture_owner').fetchall()),1)\n"
        "  db.execute('create table isolated_test(value text)');db.commit()\n"
    )
    gateway.call(
        "write_file",
        {
            "path": "test_maths.py",
            "text": tests,
            "expected_hash": None,
            "operation_key": "test-code",
        },
    )
    plan = ContainerPlan(
        IMAGE, ("python", "-I", "-m", "unittest", "discover", "-v"), ("maths.py", "test_maths.py")
    )
    runner = create({"test-suite": plan})
    database_before = hashlib.sha256(dispatch.resources.database.read_bytes()).hexdigest()
    with pytest.raises(AccessDenied, match="failed"):
        runner.execute_registered(
            "test-suite", dispatch.resources, boundary, effect, "negative-control"
        )
    current = gateway.call("read_file", {"path": "maths.py"})
    gateway.call(
        "write_file",
        {
            "path": "maths.py",
            "text": "def answer(): return 42\n",
            "expected_hash": current["sha256"],
            "operation_key": "correct-code",
        },
    )
    result = runner.execute_registered(
        "test-suite", dispatch.resources, boundary, effect, "verified-code"
    )
    assert result["outcome"] == "succeeded" and "OK" in result["output"]
    assert result["confirmed_stop"] and result["container_removed"]
    assert hashlib.sha256(dispatch.resources.database.read_bytes()).hexdigest() == database_before
    assert not (dispatch.resources.worktree / "isolated_test").exists()
    assert runner.reconcile_stopped(dispatch.attempt_id)
    for job in runner.jobs.values():
        cleanup = json.loads((job["directory"] / "cleanup.json").read_text())
        config = cleanup["container"]["configuration"]
        host = config["host"]
        assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
        assert host["Privileged"] is False and host["CapDrop"] == ["ALL"]
        assert host["SecurityOpt"] == ["no-new-privileges"]
        assert host["Memory"] == 128 * 1024 * 1024 and host["PidsLimit"] == 32
        assert config["image"] == IMAGE and len(config["mounts"]) == 2
        assert all(m["RW"] is False for m in config["mounts"])
        assert job["stop"].confirmed


@DOCKER
def test_output_limit_stops_exact_owned_container_and_retains_prefix(command_fixture):
    _, dispatch, gateway, boundary, effect, create, _ = command_fixture
    gateway.call(
        "write_file",
        {
            "path": "flood.py",
            "text": "print('x'*100000)\n",
            "expected_hash": None,
            "operation_key": "flood-code",
        },
    )
    plan = ContainerPlan(IMAGE, ("python", "-I", "flood.py"), ("flood.py",), output_bytes=8192)
    runner = create({"flood": plan})
    with pytest.raises(AccessDenied, match="output_limit"):
        runner.execute_registered("flood", dispatch.resources, boundary, effect, "bounded-output")
    job = next(iter(runner.jobs.values()))
    frames = [json.loads(line) for line in (job["directory"] / "stdout").read_text().splitlines()]
    assert frames[-1]["output_bytes_retained"] == 8192 and len(frames[-1]["output"]) == 8192
    assert runner.reconcile_stopped(dispatch.attempt_id)


@DOCKER
def test_size_limited_writable_snapshot_cannot_fill_or_modify_host_inputs(command_fixture):
    _, dispatch, gateway, boundary, effect, create, _ = command_fixture
    program = "for i in range(10):\n with open(str(i),'wb') as f: f.write(b'x'*(3*1024*1024))\n"
    gateway.call(
        "write_file",
        {"path": "disk.py", "text": program, "expected_hash": None, "operation_key": "disk-code"},
    )
    plan = ContainerPlan(IMAGE, ("python", "-I", "disk.py"), ("disk.py",))
    runner = create({"disk": plan})
    with pytest.raises(AccessDenied, match="failed"):
        runner.execute_registered("disk", dispatch.resources, boundary, effect, "bounded-disk")
    job = next(iter(runner.jobs.values()))
    assert b"No space left on device" in (job["directory"] / "stdout").read_bytes()
    assert not (dispatch.resources.worktree / "0").exists()
    assert runner.reconcile_stopped(dispatch.attempt_id)
