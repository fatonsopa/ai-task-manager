"""Private capture ordering controls; they do not establish native host readiness."""

import json
import os
import sys
import time

import pytest

from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.supervisor.models import Command
from tests.hosts.launch_evidence import CaptureFailure


def rows(request):
    path = request.node._private_launch_evidence.path
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if json.loads(line)["case_id"] == request.node.nodeid
    ]


def test_private_capture_precedes_native_control_and_input(native, request):
    process, _, _ = native()
    evidence = request.node._private_launch_evidence
    evidence.append("fixture_effect_begin", pid=process.identity.pid)
    process.write('{"op":"echo","owned_fixture":true}\n')
    assert process.read(1) == {"op": "echo", "owned_fixture": True}
    stopped = process.stop()
    assert stopped.confirmed
    events = rows(request)
    assert (
        next(i for i, row in enumerate(events) if row["event"] == "group_capture")
        < next(i for i, row in enumerate(events) if row["event"] == "control_launch")
        < next(i for i, row in enumerate(events) if row["event"] == "native_identity")
        < next(i for i, row in enumerate(events) if row["event"] == "fixture_effect_begin")
    )
    assert any(row["event"] == "group_stop" and row["confirmed_stop"] for row in events)


def test_missing_private_capture_blocks_native_launch(tmp_path, request, monkeypatch):
    marker = tmp_path / "native-must-not-start"
    command = Command(
        (sys.executable, "-I", "-c", f"open({str(marker)!r},'w').write('bad')"),
        tmp_path,
        {"PATH": os.defpath},
        2,
    )
    process = HostProcess(command, tmp_path / "output", deadline=time.time() + 5)
    evidence = request.node._private_launch_evidence
    capture = OwnedProcessGroup.capture

    def forgotten(group):
        observed = capture(group)
        evidence.known.discard(id(group.process))
        return observed

    monkeypatch.setattr(OwnedProcessGroup, "capture", forgotten)
    with pytest.raises(CaptureFailure, match="independent capture"):
        process.start()
    assert process.stop_evidence.confirmed and not marker.exists()
    events = rows(request)
    assert any(row["event"] == "control_refused" for row in events)
    assert not any(row["event"] == "control_launch" for row in events)
    assert not any(row["event"] == "native_identity" for row in events)
