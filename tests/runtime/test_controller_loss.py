import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.process import groups, worker


def test_exit_between_waitid_and_getpgid_keeps_matching_waitable_child(monkeypatch):
    pid = os.getpid() + 100000
    exited = SimpleNamespace(si_pid=pid, si_code=os.CLD_EXITED, si_status=0)
    results = iter([None, None, exited])
    monkeypatch.setattr(groups.os, "waitid", lambda *args: next(results))

    def missing(pid):
        raise ProcessLookupError()

    monkeypatch.setattr(groups.os, "getpgid", missing)
    process = SimpleNamespace(pid=pid, returncode=None)
    result = groups.OwnedProcessGroup(process).leader()
    assert result.known and result.exited and result.returncode == 0


def test_failed_signal_rechecks_owned_child_that_exited_during_stop(monkeypatch):
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(0.1)"], start_new_session=True
    )
    group = groups.OwnedProcessGroup(process)
    initial = group.capture()
    deadline = time.monotonic() + 2
    while not group.leader().exited and time.monotonic() < deadline:
        time.sleep(0.01)
    original = group.observe
    observations = iter([initial])
    monkeypatch.setattr(group, "observe", lambda: next(observations, None) or original())
    monkeypatch.setattr(group, "_signal", lambda value: False)
    try:
        stopped = group.stop()
        assert stopped.confirmed and stopped.observation.known
        assert not stopped.observation.live_members
    finally:
        group.reap_exited_leader()


@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize("resistant_descendant", [False, True])
def test_harness_stops_work_without_active_controller(tmp_path, disconnect, resistant_descendant):
    nonce = "controller-loss-fixture"
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [sys.executable, "-I", str(Path(worker.__file__)), str(write_fd), nonce, "0.8"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(write_fd,),
        start_new_session=True,
    )
    os.close(write_fd)
    group = groups.OwnedProcessGroup(process)
    group.capture()
    try:
        assert json.loads(os.read(read_fd, 4096))["nonce"] == nonce
        program = "import time;time.sleep(10)"
        if resistant_descendant:
            child = (
                "import signal,time;from pathlib import Path;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "Path('resistant-ready').touch();time.sleep(10)"
            )
            program = (
                "import subprocess,sys,os,time;"
                "assert os.read(0,1)==b'';"
                f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(10)"
            )
        process.stdin.write(
            (
                json.dumps(
                    {
                        "nonce": nonce,
                        "argv": [sys.executable, "-c", program],
                        "environment": {"PATH": os.defpath},
                        "cwd": str(tmp_path),
                        "timeout_seconds": 10,
                    }
                )
                + "\n"
            ).encode()
        )
        process.stdin.flush()
        if resistant_descendant:
            deadline = time.monotonic() + 0.5
            while not (tmp_path / "resistant-ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (tmp_path / "resistant-ready").exists()
        if disconnect:
            process.stdin.close()
        time.sleep(1)
        leader = group.leader()
        assert leader.known and leader.exited
        assert group.observe().known and not group.observe().live_members
    finally:
        os.close(read_fd)
        group.stop()
        group.reap_exited_leader()
        if not process.stdin.closed:
            process.stdin.close()
