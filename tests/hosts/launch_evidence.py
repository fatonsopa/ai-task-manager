"""Private pytest launch/capture observations; never product task authority."""

import inspect
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

MAX_BYTES = 16777216
# The observer must keep using real I/O when a case injects a product I/O fault.
_OPEN, _WRITE, _FSTAT, _FSYNC, _CLOSE = os.open, os.write, os.fstat, os.fsync, os.close


class CaptureFailure(RuntimeError):
    pass


class LaunchEvidence:
    def __init__(self, path, case_id):
        self.path, self.case_id = Path(path), case_id
        self.lock = threading.Lock()
        self.spawned = {}
        self.known = set()
        self.ever_known = set()

    def append(self, event, **data):
        caller = inspect.currentframe().f_back
        row = {
            "schema": 1,
            "event": event,
            "case_id": self.case_id,
            "observed_ns": time.monotonic_ns(),
            "caller": caller.f_code.co_filename,
            "caller_line": caller.f_lineno,
            **data,
        }
        raw = (json.dumps(row, allow_nan=False, sort_keys=True) + "\n").encode()
        if len(raw) > 65536:
            raise CaptureFailure("Capture observation exceeds its record limit")
        with self.lock:
            fd = _OPEN(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = _FSTAT(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                    or info.st_size + len(raw) > MAX_BYTES
                ):
                    raise CaptureFailure("Private launch evidence is unsafe or full")
                if _WRITE(fd, raw) != len(raw):
                    raise CaptureFailure("Private capture write is partial")
                _FSYNC(fd)
            finally:
                _CLOSE(fd)

    def group_capture(self, group, observed):
        self.append(
            "group_capture",
            pid=group.process.pid,
            group_id=group.group_id,
            start_identity=group.start_identity,
            known=observed.known,
            observation=observed.record(),
        )
        if observed.known and group.start_identity:
            self.known.add(id(group.process))
            self.ever_known.add(id(group.process))

    def before_control(self, process, data):
        if data.get("op") != "launch":
            return
        if process.group is None or id(process.process) not in self.known:
            self.append(
                "control_refused", pid=process.process.pid, reason="missing_independent_capture"
            )
            raise CaptureFailure("Native launch lacks its independent capture observation")
        self.append(
            "control_launch",
            pid=process.process.pid,
            group_id=process.group.group_id,
            start_identity=process.group.start_identity,
        )


@pytest.fixture(autouse=True)
def private_launch_evidence(request, monkeypatch, tmp_path_factory):
    from ai_task_manager.adapters.hosts.transports.process import HostProcess
    from ai_task_manager.adapters.process.groups import OwnedProcessGroup

    directory = os.environ.get("AITM_TEST_EVIDENCE_DIR")
    if not directory:
        directory = str(tmp_path_factory.mktemp("launch-evidence"))
    evidence = LaunchEvidence(Path(directory) / "launch-events.jsonl", request.node.nodeid)
    request.node.user_properties.append(("launch_evidence_path", str(evidence.path)))
    capture, stop = OwnedProcessGroup.capture, OwnedProcessGroup.stop
    start, control, popen = HostProcess.start, HostProcess._control, subprocess.Popen

    def spawned(*args, **kwargs):
        process = popen(*args, **kwargs)
        if kwargs.get("start_new_session") is True:
            # Popen can be inside Root's short boundary. Keep only object/PID
            # references here; capture/serialization occurs outside that lock.
            evidence.spawned[id(process)] = process
        return process

    def captured(group):
        observed = capture(group)
        evidence.group_capture(group, observed)
        return observed

    def stopped(group):
        observed = stop(group)
        evidence.append(
            "group_stop",
            leader_pid=group.process.pid,
            group_id=group.group_id,
            leader_start_identity=group.start_identity,
            confirmed_stop=observed.confirmed,
            signals=list(observed.signals),
            observation=observed.observation.record(),
            reason=observed.reason,
        )
        return observed

    def controlled(process, data):
        evidence.before_control(process, data)
        return control(process, data)

    def started(process):
        identity = start(process)
        try:
            evidence.append(
                "native_identity",
                pid=identity.pid,
                group_id=identity.group_id,
                start_identity=identity.start_identity,
                relay_pid=process.process.pid,
                relay_start_identity=process.group.start_identity,
            )
        except BaseException as error:
            try:
                error.owned_stop_evidence = process.stop()
            except BaseException as cleanup_error:
                error.owned_stop_error = type(cleanup_error).__name__
            raise
        return identity

    monkeypatch.setattr(subprocess, "Popen", spawned)
    monkeypatch.setattr(OwnedProcessGroup, "capture", captured)
    monkeypatch.setattr(OwnedProcessGroup, "stop", stopped)
    monkeypatch.setattr(HostProcess, "start", started)
    monkeypatch.setattr(HostProcess, "_control", controlled)
    request.node._private_launch_evidence = evidence
    try:
        yield evidence
    finally:
        missing = [
            process.pid
            for key, process in evidence.spawned.items()
            if key not in evidence.ever_known
        ]
        evidence.append("case_end", uncaptured_session_pids=missing)
