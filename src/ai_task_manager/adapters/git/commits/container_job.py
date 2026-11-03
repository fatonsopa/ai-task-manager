"""Fixed in-container Git operations on admitted actual resources; no shell API."""

import json
import os
import resource
import selectors
import signal
import subprocess
import sys
import time


def main():
    plan = json.loads(sys.argv[1])
    end = time.monotonic() + min(plan["seconds"], plan["deadline"] - time.time())
    print(json.dumps({"type": "sandbox_ready", "nonce": plan["nonce"]}), flush=True)
    ready = __import__("select").select([sys.stdin], [], [], max(0, end - time.monotonic()))[0]
    if not ready or json.loads(sys.stdin.readline(4097)) != {"nonce": plan["nonce"]}:
        return 74
    os.chdir(plan["worktree"])
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_DIR": plan["metadata"],
        "GIT_WORK_TREE": plan["worktree"],
        "GIT_INDEX_FILE": plan["index"],
        "GIT_PAGER": "cat",
        "AITM_WORKER_DATABASE": plan["database"],
        "AITM_WORKER_TASK": plan["task_id"],
        "AITM_WORKER_ATTEMPT": plan["attempt_id"],
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if plan["operation"] == "stage":
        commands = [("add", "--", *plan["files"]), ("diff", "--cached", "--check")]
    elif plan["operation"] == "commit":
        commands = [
            ("read-tree", plan["tree"]),
            ("commit", "--cleanup=verbatim", "-m", plan["message"]),
        ]
    else:
        return 74
    resource.setrlimit(resource.RLIMIT_FSIZE, (4194304, 4194304))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    output, outcome, status = bytearray(), "succeeded", None
    for command in commands:
        child = subprocess.Popen(
            ["/usr/bin/git", *command],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        streams = selectors.DefaultSelector()
        streams.register(child.stdout, selectors.EVENT_READ)
        streams.register(child.stderr, selectors.EVENT_READ)
        streams.register(sys.stdin, selectors.EVENT_READ)
        while True:
            if time.monotonic() >= end:
                outcome = "deadline"
                break
            for key, _ in streams.select(0.02):
                raw = os.read(key.fileobj.fileno(), 65536)
                if key.fileobj is sys.stdin:
                    if not raw:
                        outcome = "controller_disconnected"
                elif not raw:
                    streams.unregister(key.fileobj)
                else:
                    available = max(0, 262144 - len(output))
                    output.extend(raw[:available])
                    if len(raw) > available:
                        outcome = "output_limit"
            status = child.poll()
            if outcome != "succeeded" or (status is not None and len(streams.get_map()) == 1):
                break
        streams.close()
        if child.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
                time.sleep(0.05)
        child.wait(timeout=1)
        child.stdout.close()
        child.stderr.close()
        status = child.returncode
        if status or outcome != "succeeded":
            if outcome == "succeeded":
                outcome = "failed"
            break
    print(
        json.dumps(
            {
                "outcome": outcome,
                "returncode": status,
                "output": output.decode(errors="replace"),
                "workspace_mode": "actual_owned_worktree",
                "database_mode": "original_owned_database",
            }
        ),
        flush=True,
    )
    # All descendants, including setsid helpers, remain in this PID namespace.
    # The host watchdog removes the exact CID and verifies daemon absence.
    return 0 if outcome == "succeeded" else 74


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"outcome": "runner_error", "error": type(error).__name__}), flush=True)
        raise SystemExit(74)
