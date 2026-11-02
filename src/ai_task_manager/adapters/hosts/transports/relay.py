"""Bounded nonblocking relay; only the parent owns the native input writer."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

MAX_FRAME = 262144
OUTPUT_LIMITS = {"stdout": 4194304, "stderr": 1048576}


def terminate(reason):
    # Diagnostics must not block the watchdog behind a full stderr pipe/file.
    try:
        os.set_blocking(2, False)
        os.write(2, ("host_relay:" + reason + "\n").encode()[:256])
    except OSError:
        pass
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    time.sleep(0.1)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    ready_fd, nonce = int(sys.argv[1]), sys.argv[2]
    duration, absolute = float(sys.argv[3]), float(sys.argv[4])
    specification_fd, input_fd = int(sys.argv[5]), int(sys.argv[6])
    # These paths come from this installed relay, never a model command.
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from relay_policy import RelayPolicy, read_spec

    specification = read_spec(specification_fd)
    os.close(specification_fd)
    if specification["nonce"] != nonce:
        terminate("invalid_binding_nonce")
    command = specification["command"]
    policy = (
        RelayPolicy(specification["policy"], command)
        if specification.get("policy") is not None
        else None
    )
    deadline = time.monotonic() + duration
    current_utc = absolute
    control = b""
    child = None
    active_call = None
    exit_seen = None
    outputs = {}
    pending = {1: bytearray(), 2: bytearray()}
    totals = {1: 0, 2: 0}
    limits = {1: OUTPUT_LIMITS["stdout"], 2: OUTPUT_LIMITS["stderr"]}
    for fd in (0, 1, 2, ready_fd):
        os.set_blocking(fd, False)
    while time.monotonic() < deadline and time.time() < min(absolute, current_utc):
        if child is not None and child.poll() is not None:
            if exit_seen is None:
                exit_seen = time.monotonic()
            if (not outputs and not any(pending.values())) or time.monotonic() - exit_seen >= 0.5:
                terminate("native_exit")
        reads = [0, *outputs]
        writes = [fd for fd, data in pending.items() if data]
        ready, writable, _ = select.select(reads, writes, [], 0.02)
        for fd in writable:
            try:
                count = os.write(fd, pending[fd][:65536])
                del pending[fd][:count]
            except BlockingIOError:
                pass
            except OSError:
                terminate("output_disconnected")
        for fd in ready:
            try:
                part = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if fd != 0:
                if not part:
                    outputs.pop(fd)
                    continue
                target = outputs[fd]
                totals[target] += len(part)
                if totals[target] > limits[target]:
                    terminate("native_output_limit")
                pending[target].extend(part)
                continue
            if not part:
                terminate("controller_disconnected")
            control += part
            if len(control) > MAX_FRAME:
                terminate("oversized_control")
            while b"\n" in control:
                raw, control = control.split(b"\n", 1)
                data = json.loads(raw)
                operation = data.get("op")
                if operation == "launch" and child is None and data.get("nonce") == nonce:
                    boundary = policy.actual_spawn() if policy is not None else nullcontext(None)
                    with boundary as report:
                        child = subprocess.Popen(
                            command["argv"],
                            cwd=command["cwd"],
                            env=command["environment"],
                            stdin=input_fd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            close_fds=True,
                        )
                        if report is not None:
                            report(process_pid=None, process_start_identity=None)
                    os.close(input_fd)
                    for stream, target in ((child.stdout, 1), (child.stderr, 2)):
                        os.set_blocking(stream.fileno(), False)
                        outputs[stream.fileno()] = target
                    record = json.dumps({"nonce": nonce, "pid": child.pid}).encode() + b"\n"
                    if os.write(ready_fd, record) != len(record):
                        terminate("partial_handshake")
                    os.close(ready_fd)
                elif operation == "invocation" and child is not None and policy is not None:
                    if active_call is not None:
                        terminate("active_invocation_unreconciled")
                    # Same ledger determines the persisted bound; no caller extension.
                    current_utc = min(absolute, policy.invocation_deadline(data["call_id"]))
                    active_call = data["call_id"]
                elif operation == "complete" and child is not None and policy is not None:
                    if not policy.completed(data["call_id"], active_call=active_call):
                        terminate("unconfirmed_completion")
                    current_utc = absolute
                    active_call = None
                elif operation == "stop":
                    terminate("requested")
                else:
                    terminate("invalid_control")
    terminate("deadline")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        terminate("relay_error")
