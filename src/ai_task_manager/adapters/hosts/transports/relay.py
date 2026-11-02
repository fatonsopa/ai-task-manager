"""Credential-free stdlib relay: controller loss and deadline kill its own group."""

import json
import os
import select
import signal
import subprocess
import sys
import time

MAX_FRAME = 262144
OUTPUT_LIMITS = {"stdout": 4194304, "stderr": 1048576}


def terminate(reason):
    print(f"host_relay:{reason}", file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    time.sleep(0.1)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    ready_fd, nonce, duration = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
    deadline = time.monotonic() + duration
    control = b""
    child = None
    written = {name: 0 for name in OUTPUT_LIMITS}
    while time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            terminate("native_exit")
        reads = [sys.stdin.fileno()]
        if child is not None:
            reads.append(child.stdout.fileno())
            reads.append(child.stderr.fileno())
        ready, _, _ = select.select(reads, [], [], min(0.05, deadline - time.monotonic()))
        for fd in ready:
            if fd == sys.stdin.fileno():
                part = os.read(fd, 65536)
                if not part:
                    terminate("controller_disconnected")
                control += part
                if len(control) > MAX_FRAME:
                    terminate("oversized_control")
                while b"\n" in control:
                    raw, control = control.split(b"\n", 1)
                    data = json.loads(raw)
                    if child is None:
                        if data.get("op") != "launch" or data.get("nonce") != nonce:
                            terminate("invalid_launch")
                        child = subprocess.Popen(
                            data["argv"],
                            cwd=data["cwd"],
                            env=data["environment"],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            close_fds=True,
                        )
                        with os.fdopen(ready_fd, "w") as stream:
                            json.dump({"nonce": nonce, "pid": child.pid}, stream)
                            stream.write("\n")
                            stream.flush()
                    elif data.get("op") == "write" and not child.stdin.closed:
                        child.stdin.write(data["data"].encode())
                        child.stdin.flush()
                    elif data.get("op") == "eof" and not child.stdin.closed:
                        child.stdin.close()
                    elif data.get("op") == "stop":
                        terminate("requested")
                    else:
                        terminate("invalid_control")
            else:
                part = os.read(fd, 65536)
                if part:
                    name = "stdout" if fd == child.stdout.fileno() else "stderr"
                    available = OUTPUT_LIMITS[name] - written[name]
                    output = getattr(sys, name).buffer
                    output.write(part[:available])
                    output.flush()
                    written[name] += min(len(part), available)
                    if len(part) > available:
                        terminate("native_" + name + "_limit")
    terminate("deadline")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        terminate("relay_error")
