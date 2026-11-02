"""Owned protocol/accounting fixtures; no real native account or model calls."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.invocations import (
    InitializationInvocation,
    InvocationBinding,
    InvocationPermits,
)
from ai_task_manager.adapters.hosts.transports.native_input import NativeInput
from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.adapters.hosts.transports.relay_policy import RelayPolicy, prepare_binding
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import Command
from tests.test_build_control_effect_boundaries import SCOPE
from tests.test_build_control_policy_amendment import rig as rig
from tools.build_control import ControlError


@contextmanager
def allowed():
    yield


@pytest.fixture
def pipe():
    read_fd, write_fd = os.pipe()
    writer = NativeInput(write_fd, deadline=time.time() + 3)
    try:
        yield read_fd, writer
    finally:
        writer.close()
        os.close(read_fd)


@pytest.mark.parametrize(
    "value", [{}, {"nested": {"braces": "}"}}, {"line": "a\nb"}, {"text": "a\u2028b"}]
)
def test_incomplete_object_cannot_execute_at_eof(pipe, value):
    read_fd, writer = pipe

    @contextmanager
    def refused():
        raise AccessDenied("Authored current refusal after prefix")
        yield

    with pytest.raises(AccessDenied):
        writer.send(json.dumps(value) + "\n", boundary=refused)
    assert writer.quarantined
    writer.close()
    prefix = os.read(read_fd, 65536)
    with pytest.raises(json.JSONDecodeError):
        json.loads(prefix)


@pytest.mark.parametrize("variant", ["accepted", "deadline", "partial_commit", "commit_failure"])
def test_final_frame_commit_obeys_current_boundary(pipe, monkeypatch, variant):
    read_fd, writer = pipe
    original = os.write
    commits = []

    def write(fd, data):
        if fd == writer.fd and data == b"}\n":
            commits.append(True)
            if variant == "partial_commit":
                return original(fd, b"}")
            if variant == "commit_failure":
                raise BlockingIOError("Authored full pipe at final commit")
        return original(fd, data)

    @contextmanager
    def boundary():
        if variant == "deadline":
            writer.deadline = time.time() - 1
            raise AccessDenied("Authored current deadline refusal")
        yield

    monkeypatch.setattr(os, "write", write)
    if variant == "accepted":
        writer.send('{"actual":true}\n', boundary=boundary)
        assert json.loads(os.read(read_fd, 65536)) == {"actual": True}
        assert commits == [True] and not writer.quarantined
    else:
        with pytest.raises((AccessDenied, BlockingIOError)):
            writer.send('{"actual":true}\n', boundary=boundary)
        assert writer.quarantined
        prior = len(commits)
        with pytest.raises(AccessDenied, match="never resend"):
            writer.send("{}\n", boundary=allowed)
        assert len(commits) == prior


@pytest.mark.parametrize("text", ["[]", "{}\n\n", '{"x":NaN}', "{} {}", "1", "null"])
def test_unsupported_frame_has_no_prefix_effect(pipe, text):
    read_fd, writer = pipe
    os.set_blocking(read_fd, False)
    with pytest.raises(AccessDenied):
        writer.send(text, boundary=allowed)
    with pytest.raises(BlockingIOError):
        os.read(read_fd, 1024)
    assert not writer.quarantined


def initialized(rig, tmp_path):
    rig.now = datetime.now(UTC)
    rig.amend()
    rig.batch(seconds=60, budget="0")
    for call, purpose, worker, seconds in [
        ("host", "host", True, 35),
        ("relay", "relay", False, 30),
    ]:
        rig.current.reserve(
            call,
            label="owned protocol fixture",
            paid=False,
            max_cost_usd=None,
            uses_worker=worker,
            batch_id="batch",
            timeout_seconds=seconds,
            metadata={
                "invocation_binding": {**SCOPE, "purpose": purpose},
                "parent_call_id": "host" if call == "relay" else None,
            },
        )
        rig.current.claim_launch(call, worker_identity="owner")
    snapshot = rig.current.snapshot()
    registered = {
        row["call_id"]: InvocationBinding.observed(
            {
                "run_id": snapshot["run_id"],
                "policy_revision": 1,
                "call": row,
            }
        )
        for row in snapshot["calls"]
    }
    issuer = InvocationPermits(rig.current, registered)
    invocation = InitializationInvocation(
        issuer.prepare_launch("host"), issuer.prepare_launch("relay")
    )
    command = Command(
        (
            sys.executable,
            "-I",
            "-u",
            "-c",
            "import sys,json\nfor line in sys.stdin: print(line.strip(),flush=True)",
        ),
        tmp_path,
        {"PATH": os.defpath},
        30,
    )
    return invocation, command


@pytest.mark.parametrize("variant", ["accepted", "paused", "foreign_owner", "expired"])
def test_actual_relay_spawn_rechecks_root_policy(rig, tmp_path, variant):
    invocation, command = initialized(rig, tmp_path)
    specification = prepare_binding(invocation.native, command)
    record = {
        "argv": list(command.argv),
        "cwd": str(command.cwd),
        "environment": command.environment,
    }
    policy = RelayPolicy(specification, record)
    if variant == "paused":
        rig.admission(True)
    elif variant == "foreign_owner":
        policy.binding = replace(policy.binding, worker_identity="foreign")
    elif variant == "expired":
        policy.control.clock = lambda: datetime.fromtimestamp(
            invocation.native.binding.deadline + 1, UTC
        )
    reached = []
    if variant == "accepted":
        child = None
        try:
            with policy.actual_spawn() as report:
                child = subprocess.Popen(
                    command.argv,
                    cwd=command.cwd,
                    env=command.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                reached.append(True)
                report(process_pid=None, process_start_identity=None)
        finally:
            if child is not None:
                group = OwnedProcessGroup(child)
                group.capture()
                before, leader = group.observe(), group.leader()
                stopped = group.stop()
                evidence = Path(os.environ["AITM_TEST_EVIDENCE_DIR"])
                (evidence / "build-effect-groups-direct-spawn.json").write_text(
                    json.dumps(
                        {
                            "group": group.evidence(
                                stopped,
                                orphaned=bool(leader.exited and before.live_members),
                            )
                        }
                    )
                )
                if stopped.confirmed:
                    group.reap_exited_leader()
                assert stopped.confirmed
        assert reached == [True]
        assert invocation.native.running().binding == invocation.native.binding
    else:
        with pytest.raises((ControlError, ValueError)):
            with policy.actual_spawn():
                reached.append(True)
        assert not reached


@pytest.mark.parametrize(
    "variant",
    ["unknown_kind", "source_changed", "ledger_changed", "scope_changed", "command_changed"],
)
def test_relay_binding_refuses_untrusted_or_changed_source(rig, tmp_path, variant):
    invocation, command = initialized(rig, tmp_path)
    specification = prepare_binding(invocation.native, command)
    record = {
        "argv": list(command.argv),
        "cwd": str(command.cwd),
        "environment": command.environment,
    }
    if variant == "unknown_kind":
        specification["kind"] = "arbitrary_module"
    elif variant == "source_changed":
        specification["source_sha256"]["tools/build_control.py"] = "0" * 64
    elif variant == "ledger_changed":
        specification["ledger_identity"][1] += 1
    elif variant == "scope_changed":
        specification["binding"]["attempt_id"] = "foreign"
    else:
        record["argv"] = ["/unexpected-command"]
    with pytest.raises((AccessDenied, ValueError)):
        RelayPolicy(specification, record)


def test_actual_accounted_native_process_and_parent_input(rig, tmp_path):
    invocation, command = initialized(rig, tmp_path)
    output = tmp_path / "process"
    process = HostProcess(command, output, deadline=time.time() + 25, invocation=invocation)
    record = None
    try:
        identity = process.start()
        assert identity.pid != process.process.pid
        assert invocation.native.running().binding == invocation.native.binding
        assert invocation.relay.running().binding == invocation.relay.binding
        process.write('{"owned_fixture":true}\n')
        assert process.read(2) == {"owned_fixture": True}
        with pytest.raises(AccessDenied, match="own invocation"):
            process.write('{"type":"user","message":"forbidden"}\n')
    finally:
        if process.group is not None:
            stopped = process.stop()
            record = {
                "pid": process.process.pid,
                "group_id": process.group.group_id,
                "start_identity": process.group.start_identity,
                "group": process.group.evidence(stopped, orphaned=False),
            }
            evidence = Path(os.environ["AITM_TEST_EVIDENCE_DIR"])
            (evidence / "build-effect-groups-physical.json").write_text(json.dumps(record))
            assert stopped.confirmed


def test_parent_owned_input_backpressure_stays_outside_root(pipe):
    _, writer = pipe
    entered = []
    writer.deadline = time.time() + 0.08

    @contextmanager
    def boundary():
        entered.append(True)
        yield

    started = time.monotonic()
    with pytest.raises(AccessDenied, match="staging deadline"):
        writer.send(json.dumps({"blocked": "x" * 200000}), boundary=boundary)
    assert time.monotonic() - started < 1
    assert not entered and writer.quarantined


@pytest.mark.parametrize("variant", ["stdout", "stderr", "controller_loss"])
def test_relay_watchdog_survives_output_backpressure(tmp_path, variant):
    child_record = tmp_path / "descendant.json"
    program = (
        "import subprocess,sys,time,signal,json,os\n"
        "p=subprocess.Popen([sys.executable,'-I','-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])\n"
        f"open({str(child_record)!r},'w').write(json.dumps({{'pid':p.pid,'group':os.getpgrp()}}))\n"
        "time.sleep(.1)\n"
        + (
            f"sys.{variant}.write('x'*900000);sys.{variant}.flush()\n"
            if variant != "controller_loss"
            else ""
        )
        + "time.sleep(30)\n"
    )
    command = Command(
        (sys.executable, "-I", "-u", "-c", program), tmp_path, {"PATH": os.defpath}, 2
    )
    process = HostProcess(command, tmp_path / "output", deadline=time.time() + 1.5)
    try:
        process.start()
        until = time.monotonic() + 0.5
        while not child_record.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert child_record.exists(), "Actual descendant precondition must be reached"
        descendant = json.loads(child_record.read_text())
        assert descendant["group"] == process.group.group_id
        if variant == "controller_loss":
            process.process.stdin.close()
        until = time.monotonic() + 2.5
        while time.monotonic() < until:
            leader = process.group.leader()
            assert leader.known
            if leader.exited:
                break
            time.sleep(0.01)
        assert process.group.leader().exited
        observed = process.group.observe()
        assert observed.known and not observed.live_members
    finally:
        if process.group is not None:
            stopped = process.stop()
            evidence = Path(os.environ["AITM_TEST_EVIDENCE_DIR"])
            (evidence / f"build-effect-groups-watchdog-{variant}.json").write_text(
                json.dumps(
                    {
                        "group": process.group.evidence(stopped, orphaned=False),
                        "native_identity": process.identity.__dict__ if process.identity else None,
                        "descendant": json.loads(child_record.read_text())
                        if child_record.exists()
                        else None,
                    }
                )
            )
            assert stopped.confirmed


@pytest.mark.parametrize("variant", ["pause", "deadline"])
def test_final_native_object_commit_rechecks_root_pause(rig, pipe, variant):
    from datetime import timedelta

    from tests.hosts.test_invocation_boundaries import registered

    _, _, _, permit = registered(rig)
    read_fd, writer = pipe
    reached = []

    @contextmanager
    def boundary():
        if variant == "pause":
            rig.admission(True)
        else:
            rig.now += timedelta(seconds=31)
        with permit.effect_boundary():
            reached.append(True)
            yield

    with pytest.raises(ControlError):
        writer.send('{"actual_model_submission":true}\n', boundary=boundary)
    assert not reached and writer.quarantined
    writer.close()
    with pytest.raises(json.JSONDecodeError):
        json.loads(os.read(read_fd, 65536))
