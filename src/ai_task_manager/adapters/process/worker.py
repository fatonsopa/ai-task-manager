"""Trusted waiting harness; no assigned command executes before the pipe grant."""

import json
import os
import select
import signal
import subprocess
import sys
import time


def stop_owned_group(reason):
    print(f"waiting_harness:{reason}", file=sys.stderr, flush=True)
    # This harness creates no other session. Its own process group is the invocation
    # boundary; keep the helper alive long enough to escalate TERM-resistant children.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    time.sleep(0.05)
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    ready_fd, nonce = int(sys.argv[1]), sys.argv[2]
    deadline = time.monotonic() + float(sys.argv[3])
    with os.fdopen(ready_fd, "w") as output:
        json.dump({"type": "harness_ready", "pid": os.getpid(), "nonce": nonce}, output)
        output.write("\n")
        output.flush()
    # A closed supervisor pipe never starts work. The only input is a bounded JSON grant.
    grant = b""
    while b"\n" not in grant:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 124
        readable, _, _ = select.select([sys.stdin.fileno()], [], [], remaining)
        if not readable:
            return 124
        part = os.read(sys.stdin.fileno(), 65536)
        if not part:
            return 74
        grant += part
        if len(grant) > 1024 * 1024:
            return 74
    if len(grant) > 1024 * 1024 or not grant.endswith(b"\n"):
        return 74
    try:
        data = json.loads(grant)
        argv, environment, cwd = data["argv"], data["environment"], data["cwd"]
        if (
            data["nonce"] != nonce
            or not isinstance(argv, list)
            or not argv
            or not argv[0]
            or any(not isinstance(a, str) or "\0" in a for a in argv)
            or not isinstance(environment, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items())
        ):
            return 74
        os.chdir(cwd)
        command_deadline = min(deadline, time.monotonic() + float(data["timeout_seconds"]) + 0.2)
        child = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL)
        while child.poll() is None:
            if time.monotonic() >= command_deadline:
                stop_owned_group("deadline")
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.02)
            if readable and not os.read(sys.stdin.fileno(), 1):
                stop_owned_group("controller_disconnected")
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    except (KeyError, TypeError, ValueError, OSError):
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
