"""Real orphaned harness stopping; authority callback is an authored component control."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ai_task_manager.adapters.process.recovery import canonical, stop_waiting
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeUncertain


@pytest.fixture
def orphan(tmp_path):
    root = tmp_path.resolve()
    sockets = Path(tempfile.mkdtemp(prefix="aitm-cold-")).resolve()
    script = """
import json,time,sys
from pathlib import Path
from tests.runtime.helpers import RuntimeRig
root=Path(sys.argv[1]);r=RuntimeRig(root);r.rig.now=time.time()
r.runtime.recovery_directory=Path(sys.argv[2])
task=r.rig.queued(code=False)
decision=r.rig.decision(task,"In Progress")
dispatch=r.supervisor.start(r.rig.handle,task.id,"fixture",expected_version=task.version,
 decision_revision=task.decision_revision,decision_ref=decision,operation_key="cold-owned",
 deadline=r.rig.now+120)
(root/"ready.json").write_text(json.dumps({"attempt_id":dispatch.attempt_id}))
while True:time.sleep(.05)
"""
    out = (root / "owner.stdout").open("wb")
    err = (root / "owner.stderr").open("wb")
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(sockets)],
        stdout=out,
        stderr=err,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    limit = time.monotonic() + 10
    while not (root / "ready.json").exists() and child.poll() is None and time.monotonic() < limit:
        time.sleep(0.03)
    assert (root / "ready.json").exists(), (root / "owner.stderr").read_text()
    attempt_id = json.loads((root / "ready.json").read_text())["attempt_id"]
    child.kill()
    child.wait(timeout=5)
    assert child.returncode == -signal.SIGKILL
    journal = RuntimeJournal(root / "runtime.sqlite")
    try:
        yield journal, attempt_id
    finally:
        # The product stop-only channel, never a PID signal by this cold caller.
        try:
            proof = stop_waiting(journal, attempt_id, check_authority=lambda: None)
            assert journal.observation(proof)["quiescent"] is True
            from ai_task_manager.adapters.workflow_control.identity import endpoint_binding

            control = journal.effect(attempt_id, "launch")["recovery"]
            witness = journal.observations(attempt_id, "harness_ready")[0]["recovery_control"]
            assert endpoint_binding(control["path"]) == witness
            Path(control["path"]).unlink()
            sockets.rmdir()
        finally:
            out.close()
            err.close()


def test_cold_stop_uses_original_harness_and_preserves_runtime_intent(orphan):
    journal, attempt = orphan
    original = journal.effect(attempt, "launch")
    process = journal.observations(attempt, "process_created")[0]
    command = subprocess.check_output(
        ["ps", "-p", str(process["pid"]), "-o", "command="], text=True
    )
    assert original["recovery"]["token"] not in command
    assert original["recovery"]["token"] not in json.dumps(
        journal.observations(attempt, "harness_ready")
    )
    before = len(journal.observations(attempt, "process_created"))
    checks = []
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(10)"], start_new_session=True
    )
    try:
        proof = stop_waiting(journal, attempt, check_authority=lambda: checks.append(True))
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=3)
    result = journal.observation(proof, attempt_id=attempt, kind="cold_harness_stop")
    assert original["recovery"]["token"] not in json.dumps(result)
    assert result["quiescent"] is True and result["model_success"] == "not_established"
    assert result["signals_by_recovery_caller"] == [] and checks
    assert result["original_process_observation"] == process["id"]
    assert journal.effect(attempt, "launch") == original
    assert len(journal.observations(attempt, "process_created")) == before == 1
    assert stop_waiting(journal, attempt, check_authority=lambda: None) == proof
    assert len(journal.observations(attempt, "cold_harness_stop")) == 1


def test_stop_channel_refuses_execution_and_forged_token_before_valid_stop(orphan):
    journal, attempt = orphan
    control = journal.effect(attempt, "launch")["recovery"]
    process = journal.observations(attempt, "process_created")[0]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(1)
        stream.connect(control["path"])
        stream.sendall(b"[]\n")
        assert stream.recv(4096) == b""
    for action in ["execute", "stop"]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(1)
            stream.connect(control["path"])
            stream.sendall(
                canonical(
                    {
                        "action": action,
                        "attempt_id": attempt,
                        "pid": process["pid"],
                        "nonce": "0" * 32,
                        "mac": "0" * 64,
                    }
                )
                + b"\n"
            )
            assert stream.recv(4096) == b""
    assert journal.effect(attempt, "work_grant") is None
    proof = stop_waiting(journal, attempt, check_authority=lambda: None)
    assert journal.observation(proof)["quiescent"] is True


def test_current_stop_authority_refusal_sends_no_request(orphan):
    journal, attempt = orphan

    def refused():
        raise RuntimeRefused("Authored expired stop permission")

    with pytest.raises(RuntimeRefused):
        stop_waiting(journal, attempt, check_authority=refused)
    assert journal.observations(attempt, "cold_harness_stop") == []
    assert journal.effect(attempt, "work_grant") is None


@pytest.mark.parametrize("condition", ["unknown", "reused"])
def test_unknown_or_changed_process_identity_remains_protected(orphan, monkeypatch, condition):
    from ai_task_manager.adapters.process import recovery
    from ai_task_manager.adapters.process.groups import GroupObservation, ProcessMember

    journal, attempt = orphan
    process = journal.observations(attempt, "process_created")[0]
    observed = GroupObservation(False, reason="Authored unavailable inventory")
    if condition == "reused":
        observed = GroupObservation(
            True,
            (ProcessMember(process["pid"], process["group"], "S", "different-original-start"),),
        )
    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_inventory", lambda: observed)
        with pytest.raises(RuntimeUncertain):
            stop_waiting(journal, attempt, check_authority=lambda: None)
    assert journal.observations(attempt, "cold_harness_stop") == []
