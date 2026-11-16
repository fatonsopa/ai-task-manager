"""Stop a recorded waiting harness through its own retained stop-only endpoint."""

import hashlib
import hmac
import json
import math
import secrets
import socket
import time
from pathlib import Path

from ai_task_manager.adapters.workflow_control.identity import endpoint_binding
from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeUncertain

from .groups import GroupObservation, _inventory


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def mac(token, value):
    return hmac.new(bytes.fromhex(token), canonical(value), hashlib.sha256).hexdigest()


def observe_recorded_group(pid, start_identity, group):
    """Observe only; a matching numeric PID never grants cold signal authority."""
    observed = _inventory()
    if not observed.known:
        return observed
    leader = next((p for p in observed.members if p.pid == pid), None)
    members = tuple(p for p in observed.members if p.group == group)
    if leader is None:
        return GroupObservation(
            not members, members, "leader_missing_with_members" if members else None
        )
    if leader.group != group or leader.start_identity != start_identity:
        return GroupObservation(False, reason="recorded_leader_reused_or_changed")
    return GroupObservation(True, members)


def stop_waiting(journal, attempt_id, *, check_authority, timeout=4):
    """A trusted current authority check is mandatory; this API grants no execution."""
    if (
        not callable(check_authority)
        or type(timeout) not in {int, float}
        or not math.isfinite(timeout)
    ):
        raise RuntimeRefused("Current finite stop authority is required")
    if not 0 < timeout <= 4:
        raise RuntimeRefused("Stop observation must use the bounded group-stop interval")
    deadline = time.monotonic() + timeout
    check_authority()
    intent = journal.effect(attempt_id, "launch")
    records = journal.observations(attempt_id, "process_created")
    if not intent or "recovery" not in intent or len(records) != 1:
        raise RuntimeUncertain("Original waiting-harness recovery identity is unavailable")
    control = intent["recovery"]
    ready = journal.observations(attempt_id, "harness_ready")
    if len(ready) != 1 or "recovery_control" not in ready[0]:
        raise RuntimeUncertain("Original waiting-harness endpoint observation is unavailable")
    process = records[0]
    if process.get("worker_id") != journal.attempt(attempt_id)["worker_id"]:
        raise RuntimeUncertain("Original waiting-harness owner differs")
    if (
        control["source_sha256"]
        != hashlib.sha256(Path(__file__).with_name("worker.py").read_bytes()).hexdigest()
    ):
        raise RuntimeRefused("Waiting-harness recovery source changed")
    pid, start, group = process["pid"], process["start_identity"], process["group"]
    before = observe_recorded_group(pid, start, group)
    if not before.known:
        raise RuntimeUncertain("Recorded waiting-harness process ownership is unknown")
    if before.live_members:
        if endpoint_binding(control["path"]) != ready[0]["recovery_control"]:
            raise RuntimeUncertain("Original waiting-harness endpoint changed")
        check_authority()
        body = {
            "action": "stop",
            "attempt_id": attempt_id,
            "pid": pid,
            "nonce": secrets.token_hex(16),
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(max(0.001, deadline - time.monotonic()))
                stream.connect(control["path"])
                check_authority()
                stream.sendall(canonical(dict(body, mac=mac(control["token"], body))) + b"\n")
                raw = bytearray()
                while b"\n" not in raw and len(raw) <= 4096:
                    part = stream.recv(4097 - len(raw))
                    if not part:
                        break
                    raw.extend(part)
            response = json.loads(raw)
        except (OSError, ValueError) as error:
            raise RuntimeUncertain("Waiting-harness stop acknowledgement is unavailable") from error
        expected = dict(body, state="stopping")
        if response != dict(expected, mac=mac(control["token"], expected)):
            raise RuntimeUncertain("Waiting-harness stop acknowledgement is invalid")
    while True:
        check_authority()
        after = observe_recorded_group(pid, start, group)
        if after.known and not after.live_members:
            check_authority()
            prior = journal.observations(attempt_id, "cold_harness_stop")
            if prior and prior[-1].get("original_process_observation") == process["id"]:
                return prior[-1]["id"]
            return journal.observe(
                attempt_id,
                "cold_harness_stop",
                {
                    "quiescent": True,
                    "worker_id": process["worker_id"],
                    "original_process_observation": process["id"],
                    "group": after.record(),
                    "signals_by_recovery_caller": [],
                    "model_success": "not_established",
                },
            )
        if time.monotonic() >= deadline:
            raise RuntimeUncertain("Waiting-harness stop remains unconfirmed")
        time.sleep(0.02)
