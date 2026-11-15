"""Completion identity and deterministic final-output observations with owned children."""

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding, InvocationPermits
from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.adapters.hosts.transports.relay_policy import RelayPolicy, prepare_binding
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.test_physical_invocations import initialized
from tests.test_build_control_effect_boundaries import SCOPE
from tests.test_build_control_policy_amendment import rig as rig
from tools.build_control import ControlError


def live_parent(rig, name):
    rig.current.reserve(
        name,
        label="authored foreign host fixture",
        paid=False,
        max_cost_usd=None,
        uses_worker=True,
        batch_id="batch",
        timeout_seconds=20,
        metadata={"invocation_binding": {**SCOPE, "purpose": "host"}},
    )
    rig.current.claim_launch(name, worker_identity="owner")
    rig.current.mark_started(name, worker_identity="owner")


def complete_call(rig, name, parent, *, scope=None, worker="owner", outcome="succeeded"):
    rig.current.reserve(
        name,
        label="authored terminal fixture",
        paid=False,
        max_cost_usd=None,
        uses_worker=False,
        batch_id="batch",
        timeout_seconds=10,
        metadata={"parent_call_id": parent, "invocation_binding": scope or SCOPE},
    )
    rig.current.claim_launch(name, worker_identity=worker)
    call = rig.current.mark_started(name, worker_identity=worker)
    snapshot = rig.current.snapshot()
    binding = InvocationBinding.observed(
        {"run_id": snapshot["run_id"], "policy_revision": 1, "call": call}
    )
    issuer = InvocationPermits(rig.current, {name: binding})
    permit = issuer.prepare(name)
    rig.current.record_outcome(
        name,
        outcome=outcome,
        process_stopped=outcome == "succeeded",
        actual_cost_usd="0" if outcome == "succeeded" else None,
        evidence_ref="authored terminal logical operation, no model",
    )
    return permit


def test_foreign_completed_call_cannot_clear_active_bound(rig, tmp_path):
    invocation, command = initialized(rig, tmp_path)
    live_parent(rig, "another-native-host")
    foreign = complete_call(rig, "foreign", "another-native-host")
    spec = prepare_binding(invocation.native, command)
    policy = RelayPolicy(
        spec,
        {"argv": list(command.argv), "cwd": str(command.cwd), "environment": command.environment},
    )
    assert policy.completed(foreign.binding.call_id) is False


def test_parent_completion_requires_exact_pending_capability(rig, tmp_path, monkeypatch):
    invocation, command = initialized(rig, tmp_path)
    live_parent(rig, "another-native-host")
    foreign = complete_call(rig, "foreign", "another-native-host")
    process = HostProcess(
        command, tmp_path / "output", deadline=time.time() + 10, invocation=invocation
    )
    process._pending_completion = invocation.native
    process._pending_submitted = True
    calls = []
    monkeypatch.setattr(process, "_control", lambda value: calls.append(value))
    with pytest.raises(AccessDenied):
        process.complete_invocation(foreign)
    assert not calls


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "wrong_active",
        "unknown",
        "parent",
        "owner",
        "attempt_id",
        "host",
        "configuration_digest",
        "model",
        "effort",
        "stale",
    ],
)
def test_relay_completion_rechecks_exact_active_scope(rig, tmp_path, variant):
    from datetime import UTC, datetime

    invocation, command = initialized(rig, tmp_path)
    scope = dict(SCOPE)
    if variant in {"attempt_id", "configuration_digest", "model", "effort"}:
        scope[variant] = "foreign"
    elif variant == "host":
        scope["host"] = "claude"
    if variant == "parent":
        live_parent(rig, "foreign")
    if variant == "owner":
        # Current launch admission rejects a foreign owner before it can
        # produce the terminal child receipt assumed by the older fixture.
        before = next(row for row in rig.current.snapshot()["calls"] if row["call_id"] == "host")
        with pytest.raises(ControlError) as caught:
            complete_call(rig, "current", "host", scope=scope, worker="foreign")
        assert caught.value.code == "parent_call_conflict"
        call = next(row for row in rig.current.snapshot()["calls"] if row["call_id"] == "current")
        assert call["status"] == "reserved" and call["process_stopped"] is False
        after = next(row for row in rig.current.snapshot()["calls"] if row["call_id"] == "host")
        assert after == before
        policy = RelayPolicy(
            prepare_binding(invocation.native, command),
            {
                "argv": list(command.argv),
                "cwd": str(command.cwd),
                "environment": command.environment,
            },
        )
        assert policy.completed("current", active_call="current") is False
        return
    permit = complete_call(
        rig,
        "current",
        "foreign" if variant == "parent" else "host",
        scope=scope,
        worker="foreign" if variant == "owner" else "owner",
        outcome="unknown" if variant == "unknown" else "succeeded",
    )
    policy = RelayPolicy(
        prepare_binding(invocation.native, command),
        {"argv": list(command.argv), "cwd": str(command.cwd), "environment": command.environment},
    )
    if variant == "stale":
        policy.control.clock = lambda: datetime.fromtimestamp(permit.binding.deadline + 1, UTC)
    assert policy.completed(
        "current", active_call="other" if variant == "wrong_active" else "current"
    ) is (variant == "valid")


def test_parent_exact_completion_is_single_use(rig, tmp_path, monkeypatch):
    invocation, command = initialized(rig, tmp_path)
    permit = complete_call(rig, "current", "host")
    process = HostProcess(
        command, tmp_path / "output", deadline=time.time() + 10, invocation=invocation
    )
    process._pending_completion, process._pending_submitted = permit, True
    calls = []
    monkeypatch.setattr(process, "_control", lambda value: calls.append(value))
    process.complete_invocation(permit)
    assert calls == [{"op": "complete", "call_id": "current"}]
    with pytest.raises(AccessDenied):
        process.complete_invocation(permit)
    assert len(calls) == 1


def test_final_output_is_drained_after_actual_child_exit(tmp_path):
    root = Path(__file__).parents[2]
    source = (
        "import sys,subprocess\n"
        f"sys.path.insert(0,{str(root / 'src')!r})\n"
        "from ai_task_manager.adapters.hosts.transports import relay\n"
        "real=relay.subprocess.Popen\n"
        "def observed(*args,**kwargs):\n"
        " child=real(*args,**kwargs)\n"
        " child.wait(timeout=2)\n"
        " return child\n"
        "relay.subprocess.Popen=observed\n"
        "sys.argv=['relay',*sys.argv[1:]]\n"
        "relay.main()\n"
    )
    ready_read, ready_write = os.pipe()
    native_read, native_write = os.pipe()
    group = None
    process = None
    nonce = "owned-final-output"
    command = {
        "argv": [sys.executable, "-I", "-c", 'print(\'{"final":"retained"}\',flush=True)'],
        "cwd": str(tmp_path),
        "environment": {"PATH": os.defpath},
    }
    try:
        with tempfile.TemporaryFile(dir=tmp_path) as spec:
            os.fchmod(spec.fileno(), 0o600)
            spec.write(json.dumps({"nonce": nonce, "command": command, "policy": None}).encode())
            spec.flush()
            spec.seek(0)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    source,
                    str(ready_write),
                    nonce,
                    "3",
                    str(time.time() + 3),
                    str(spec.fileno()),
                    str(native_read),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmp_path,
                env={"PATH": os.defpath},
                pass_fds=(ready_write, spec.fileno(), native_read),
                start_new_session=True,
            )
        group = OwnedProcessGroup(process)
        assert group.capture().known
        os.close(ready_write)
        ready_write = -1
        os.close(native_read)
        native_read = -1
        process.stdin.write((json.dumps({"op": "launch", "nonce": nonce}) + "\n").encode())
        process.stdin.flush()
        assert select.select([ready_read], [], [], 3)[0]
        handshake = json.loads(os.read(ready_read, 4096))
        assert handshake["nonce"] == nonce
        chunks = []
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if select.select([process.stdout], [], [], 0.1)[0]:
                part = os.read(process.stdout.fileno(), 4096)
                if not part:
                    break
                chunks.append(part)
        assert b"".join(chunks) == b'{"final":"retained"}\n'
    finally:
        for fd in (ready_read, ready_write, native_read, native_write):
            if fd >= 0:
                os.close(fd)
        if group is not None:
            stopped = group.stop()
            evidence = group.evidence(stopped, orphaned=False)
            Path(
                os.environ["AITM_TEST_EVIDENCE_DIR"], "build-effect-groups-final-output.json"
            ).write_text(json.dumps({"group": evidence}))
            if stopped.confirmed:
                group.reap_exited_leader()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            assert stopped.confirmed
