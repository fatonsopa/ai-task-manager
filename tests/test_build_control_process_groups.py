"""Real owned-group regressions; every test cleans up only its own fixtures."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools.build_control import BuildControl, ControlError, run_command

CHILD = """
import json,os,signal,subprocess,sys,time
from pathlib import Path
if sys.argv[2] == 'ignore':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
identity = subprocess.check_output(['ps','-o','lstart=','-p',str(os.getpid())],text=True).strip()
target=Path(sys.argv[1])
temporary=target.with_suffix('.tmp')
temporary.write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp(),'start':identity}))
os.replace(temporary,target)
time.sleep(20)
"""

LEADER = """
import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]])
deadline=time.monotonic()+5
while not Path(sys.argv[2]).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(44)
    time.sleep(.01)
if sys.argv[4] == 'sleep':
    time.sleep(20)
raise SystemExit(int(sys.argv[4]))
"""


def identity_and_state(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True
    )
    if result.returncode or not result.stdout.strip():
        return None, None
    state, identity = result.stdout.strip().split(None, 1)
    return state, " ".join(identity.split())


def live(pid):
    state, _ = identity_and_state(pid)
    return state is not None and not state.startswith(("Z", "X"))


def cleanup_fixture(ready):
    if not ready.exists():
        return
    fixture = json.loads(ready.read_text())
    state, identity = identity_and_state(fixture["pid"])
    try:
        if (
            state is not None
            and not state.startswith(("Z", "X"))
            and identity == " ".join(fixture["start"].split())
            and os.getpgid(fixture["pid"]) == fixture["group"]
        ):
            os.killpg(fixture["group"], signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.fixture
def control(tmp_path):
    result = BuildControl(tmp_path / "ledger.sqlite")
    result.initialize(run_id="group-regression", started_at=datetime.now(UTC))
    return result


@pytest.fixture
def foreign_group():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


def leader_command(ready, *, ignore_term=False, exit_code="0"):
    return [
        sys.executable,
        "-c",
        LEADER,
        CHILD,
        str(ready),
        "ignore" if ignore_term else "normal",
        exit_code,
    ]


@pytest.mark.parametrize("ignore_term", [False, True])
@pytest.mark.parametrize("exit_code", ["0", "7"])
def test_orphaned_group_never_claims_success_or_leaves_live_children(
    control, tmp_path, foreign_group, ignore_term, exit_code
):
    ready = tmp_path / "child.json"
    try:
        result = run_command(
            control,
            call_id="orphan",
            command=leader_command(ready, ignore_term=ignore_term, exit_code=exit_code),
            paid=True,
            max_cost_usd="3",
            timeout_seconds=5,
            output_directory=tmp_path / "traces",
        )
        child = json.loads(ready.read_text())
        assert foreign_group.poll() is None
        assert result["returncode"] == int(exit_code)
        assert result["call"]["status"] == "failed"
        assert result["call"]["reason"].startswith("orphaned_process_group")
        assert result["call"]["process_stopped"] is True
        assert not live(child["pid"])
        assert control.snapshot()["active_workers"] == 0
        assert control.snapshot()["reserved_usd"] == "3.000000"
        assert result["group"]["orphaned"] is True
        assert result["group"]["confirmed_stop"] is True
        assert "SIGTERM" in result["group"]["signals"]
        if ignore_term:
            assert "SIGKILL" in result["group"]["signals"]
        evidence = Path(result["group_evidence"])
        assert evidence.stat().st_mode & 0o777 == 0o600
        assert json.loads(evidence.read_text())["group"] == result["group"]
    finally:
        cleanup_fixture(ready)


def test_timeout_confirms_term_resistant_group_stop_before_releasing_worker(
    control, tmp_path, foreign_group
):
    ready = tmp_path / "child.json"
    try:
        result = run_command(
            control,
            call_id="group-timeout",
            command=leader_command(ready, ignore_term=True, exit_code="sleep"),
            paid=True,
            max_cost_usd="2",
            timeout_seconds=2,
            output_directory=tmp_path / "traces",
        )
        child = json.loads(ready.read_text())
        assert foreign_group.poll() is None
        assert result["call"]["status"] == "failed"
        assert result["call"]["outcome"] == "interrupted"
        assert result["call"]["reason"] == "timeout"
        assert result["group"]["confirmed_stop"] is True
        assert "SIGKILL" in result["group"]["signals"]
        assert not live(child["pid"])
        assert control.snapshot()["active_workers"] == 0
        assert control.snapshot()["reserved_usd"] == "2.000000"
    finally:
        cleanup_fixture(ready)


def test_unavailable_group_inventory_is_unknown_and_keeps_reservations(
    control, tmp_path, monkeypatch, foreign_group
):
    from tools.build_support import process_group

    monkeypatch.setattr(process_group, "TERM_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(process_group, "KILL_CONFIRM_SECONDS", 0.05)
    monkeypatch.setattr(
        process_group.OwnedProcessGroup,
        "observe",
        lambda self: process_group.GroupObservation(
            known=False, reason="injected inventory failure"
        ),
    )
    result = run_command(
        control,
        call_id="unconfirmed-group",
        command=[sys.executable, "-c", "import time; time.sleep(20)"],
        paid=True,
        max_cost_usd="2",
        timeout_seconds=1,
        output_directory=tmp_path / "traces",
    )
    assert foreign_group.poll() is None
    assert result["call"]["status"] == "unknown"
    assert result["call"]["process_stopped"] is False
    assert result["group"]["confirmed_stop"] is False
    assert result["group"]["observation"]["known"] is False
    assert control.snapshot()["active_workers"] == 1
    assert control.snapshot()["reserved_usd"] == "2.000000"
    assert not live(result["group"]["group_id"])


def test_start_record_error_cleans_only_the_owned_group(
    control, tmp_path, monkeypatch, foreign_group
):
    ready = tmp_path / "child.json"

    def fail_after_child_started(*args, **kwargs):
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        raise ControlError("injected_record_failure", "A controlled start-record failure.")

    monkeypatch.setattr(control, "mark_started", fail_after_child_started)
    try:
        with pytest.raises(ControlError) as raised:
            run_command(
                control,
                call_id="record-error",
                command=leader_command(ready, ignore_term=True, exit_code="sleep"),
                paid=True,
                max_cost_usd="2",
                timeout_seconds=5,
                output_directory=tmp_path / "traces",
            )
        assert raised.value.code == "injected_record_failure"
        child = json.loads(ready.read_text())
        assert foreign_group.poll() is None
        assert not live(child["pid"])
        call = control.snapshot()["calls"][0]
        assert call["status"] == "failed"
        assert call["process_stopped"] is True
        assert control.snapshot()["active_workers"] == 0
        assert control.snapshot()["reserved_usd"] == "2.000000"
    finally:
        cleanup_fixture(ready)
