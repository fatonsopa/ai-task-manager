"""Trusted in-container runner; editable code is its separate child process."""

import json
import math
import os
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import time
import traceback


def stop(group):
    for value in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, value)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def main():
    plan = json.loads(sys.argv[1])
    remaining = min(plan["seconds"], plan["deadline"] - time.time())
    if remaining <= 0:
        return 124
    end = time.monotonic() + remaining
    print(json.dumps({"type": "sandbox_ready", "nonce": plan["nonce"]}), flush=True)
    remaining = end - time.monotonic()
    if remaining <= 0 or not __import__("select").select([sys.stdin], [], [], remaining)[0]:
        return 124
    grant = sys.stdin.readline(4097)
    if not grant or len(grant) > 4096 or json.loads(grant) != {"nonce": plan["nonce"]}:
        return 74
    shutil.copytree("/input", "/workspace", dirs_exist_ok=True, symlinks=False)
    shutil.copyfile("/input-db.sqlite", "/database/task.sqlite")
    os.chdir("/workspace")

    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (plan["file_bytes"], plan["file_bytes"]))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(
            resource.RLIMIT_CPU, (max(1, math.ceil(remaining)), max(1, math.ceil(remaining) + 1))
        )

    child = subprocess.Popen(
        plan["argv"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=limits,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "TASK_DATABASE": "/database/task.sqlite",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    output = bytearray()
    outcome = None
    selected = selectors.DefaultSelector()
    selected.register(child.stdout, selectors.EVENT_READ)
    selected.register(child.stderr, selectors.EVENT_READ)
    selected.register(sys.stdin, selectors.EVENT_READ)
    while outcome is None:
        if time.monotonic() >= end:
            outcome = "deadline"
        status = child.poll()
        for key, _ in selected.select(0.02):
            part = os.read(key.fileobj.fileno(), 65536)
            if key.fileobj is sys.stdin:
                if not part:
                    outcome = "controller_disconnected"
                continue
            if not part:
                selected.unregister(key.fileobj)
            else:
                available = max(0, plan["output_bytes"] - len(output))
                output.extend(part[:available])
                if len(part) > available:
                    outcome = "output_limit"
        if status is not None and not any(
            key.fileobj is not sys.stdin for key in selected.get_map().values()
        ):
            try:
                os.killpg(child.pid, 0)
                outcome = "orphaned_child"
            except ProcessLookupError:
                outcome = "succeeded" if status == 0 else "failed"
    stop(child.pid)
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        outcome = "unknown"
    print(
        json.dumps(
            {
                "outcome": outcome,
                "returncode": child.returncode,
                "output": output.decode(errors="replace"),
                "output_bytes_retained": len(output),
                "deadline": plan["deadline"],
                "database_mode": "command_snapshot",
                "workspace_mode": "size_limited_snapshot",
            }
        ),
        flush=True,
    )
    return 0 if outcome == "succeeded" else 74


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "outcome": "runner_error",
                    "error": type(error).__name__,
                    "detail": str(error),
                    "traceback": traceback.format_exc(limit=3),
                }
            ),
            flush=True,
        )
        raise SystemExit(74)
