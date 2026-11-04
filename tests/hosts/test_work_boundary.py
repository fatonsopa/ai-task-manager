import json
import os
import selectors
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

import ai_task_manager
from ai_task_manager.domains.work.errors import InvalidTransition
from tests.domains.helpers import Rig


def hold_process(rig, task, tmp_path):
    ref = rig.proof(
        "human_decision", task.id, {"previous_revision": task.decision_revision, "hold": True}
    )
    payload = {
        "database": rig.db.path,
        "runtime_root": str(Path(ai_task_manager.__file__).resolve().parent),
        "import_record": str(tmp_path / "child-import.json"),
        "policy": asdict(rig.policy),
        "proof": asdict(rig.proofs.evidence[ref]),
        "handle": rig.handle.token,
        "task_id": task.id,
        "version": task.version,
    }
    path = tmp_path / "hold.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return subprocess.Popen(
        [sys.executable, "-m", "tests.hosts.authority_process", str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(ai_task_manager.__file__).resolve().parent.parent),
        },
    )


def test_independent_hold_commits_first_prevents_file_effect(tmp_path):
    r = Rig(tmp_path / "core.sqlite")
    task, attempt = r.running()
    child = hold_process(r, r.current(task), tmp_path)
    assert child.stdout.readline().strip() == "READY"
    stdout, stderr = child.communicate(input="GO\n", timeout=5)
    assert child.returncode == 0, (stdout, stderr)
    assert json.loads((tmp_path / "child-import.json").read_text())["runtime_root"] == str(
        Path(ai_task_manager.__file__).resolve().parent
    )
    output = tmp_path / "must-not-exist"
    with pytest.raises(InvalidTransition):
        with r.core.execution.work_boundary(r.handle, attempt.id, attempt.version):
            output.write_text("forbidden")
    assert not output.exists()


def test_short_effect_lock_precedes_independent_hold_and_later_effect_refuses(tmp_path):
    r = Rig(tmp_path / "core.sqlite")
    task, attempt = r.running()
    output = tmp_path / "permitted"
    child = hold_process(r, r.current(task), tmp_path)
    assert child.stdout.readline().strip() == "READY"
    with r.core.execution.work_boundary(r.handle, attempt.id, attempt.version):
        child.stdin.write("GO\n")
        child.stdin.flush()
        # The child's actual second SQLite connection is open before it attempts BEGIN.
        with selectors.DefaultSelector() as selected:
            selected.register(child.stdout, selectors.EVENT_READ)
            assert selected.select(3)
            assert child.stdout.readline().strip() == "CONNECT"
        assert child.poll() is None
        output.write_text("published-before-hold")
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, (stdout, stderr)
    assert json.loads((tmp_path / "child-import.json").read_text())["runtime_root"] == str(
        Path(ai_task_manager.__file__).resolve().parent
    )
    assert "COMMITTED" in stdout and output.read_text() == "published-before-hold"
    with pytest.raises(InvalidTransition):
        with r.core.execution.work_boundary(r.handle, attempt.id, attempt.version):
            output.write_text("forbidden-after-hold")
    assert output.read_text() == "published-before-hold"
