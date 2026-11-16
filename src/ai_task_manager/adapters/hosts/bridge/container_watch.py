"""Host watchdog: an untrusted container cannot kill its deadline/cleanup owner."""

import hashlib
import hmac
import json
import os
import resource
import runpy
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def publish(path, value):
    temporary = Path(str(path) + ".tmp")
    with temporary.open("x") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main():
    resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152))
    plan = json.loads(Path(sys.argv[1]).read_text())
    cleanup = runpy.run_path(str(Path(__file__).with_name("container_inspection.py")))["cleanup"]
    stop_requested = []
    signal.signal(signal.SIGTERM, lambda *_: stop_requested.append("signal"))
    child = None
    result = {"watcher_pid": os.getpid(), "owner": plan["owner"], "name": plan["name"]}
    try:
        child = subprocess.Popen(plan["argv"], env=plan["environment"], stdin=subprocess.PIPE)
        while child.poll() is None:
            if stop_requested:
                result["trigger"] = "stop_signal"
                break
            if time.time() >= plan["deadline"]:
                result["trigger"] = "deadline"
                break
            if select.select([sys.stdin], [], [], 0.02)[0]:
                data = os.read(sys.stdin.fileno(), 4097)
                if not data:
                    result["trigger"] = "controller_disconnected"
                    break
                if len(data) > 4096:
                    result["trigger"] = "invalid_controller_frame"
                    break
                child.stdin.write(data)
                child.stdin.flush()
        if child.returncode is not None:
            result["child_returncode"] = child.returncode
            result["trigger"] = "cli_exit"
    except Exception as error:
        result["error"] = type(error).__name__
    finally:
        try:
            result["container"] = cleanup(plan)
        except Exception as error:
            result["container"] = {"confirmed_stop": False, "error": type(error).__name__}
        if "recovery_key" in plan:
            raw = json.dumps(
                result, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            result["recovery_mac"] = hmac.new(
                bytes.fromhex(plan["recovery_key"]), raw, hashlib.sha256
            ).hexdigest()
        publish(plan["result"], result)
        # Keep our own anchor alive through cleanup, then terminate only this group.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(0.05)
        os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    main()
