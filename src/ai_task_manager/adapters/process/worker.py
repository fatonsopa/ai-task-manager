"""Trusted waiting harness; no assigned command executes before the pipe grant."""

import hashlib
import hmac
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def stop_owned_group(reason):
    print(f"waiting_harness:{reason}", file=sys.stderr, flush=True)
    # This harness creates no other session. Its own process group is the invocation
    # boundary; keep the helper alive long enough to escalate TERM-resistant children.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    time.sleep(0.05)
    os.killpg(os.getpgrp(), signal.SIGKILL)


class RecoveryControl:
    """This original process can stop its own group; it cannot accept work here."""

    def __init__(self, specification):
        self.specification = specification
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = Path(specification["path"])
        if (
            not path.is_absolute()
            or path.parent.resolve() != path.parent
            or path.parent.stat().st_uid != os.getuid()
            or path.parent.stat().st_mode & 0o077
        ):
            raise ValueError("Private physical recovery endpoint required")
        self.listener.bind(str(path))
        path.chmod(0o600)
        info = path.stat()
        self.binding = {"path": str(path), "device": info.st_dev, "inode": info.st_ino}
        self.listener.listen(2)

    def stop_if_requested(self):
        try:
            return self._stop_if_requested()
        except (ValueError, TypeError, KeyError, OSError):
            return

    def _stop_if_requested(self):
        stream, _ = self.listener.accept()
        with stream:
            stream.settimeout(0.2)
            raw = bytearray()
            while b"\n" not in raw and len(raw) <= 4096:
                part = stream.recv(4097 - len(raw))
                if not part:
                    return
                raw.extend(part)
            try:
                request = json.loads(raw)
                if type(request) is not dict:
                    return
                body = {k: v for k, v in request.items() if k != "mac"}

                def canonical(value):
                    return json.dumps(
                        value, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ).encode()

                def sign(value):
                    return hmac.new(
                        bytes.fromhex(self.specification["token"]), canonical(value), hashlib.sha256
                    ).hexdigest()

                if (
                    set(request) != {"action", "attempt_id", "pid", "nonce", "mac"}
                    or body["action"] != "stop"
                    or body["pid"] != os.getpid()
                    or body["attempt_id"] != self.specification["attempt_id"]
                    or type(body["nonce"]) is not str
                    or len(body["nonce"]) != 32
                    or not hmac.compare_digest(request["mac"], sign(body))
                ):
                    return
                response = dict(body, state="stopping")
                stream.sendall(canonical(dict(response, mac=sign(response))) + b"\n")
            except (ValueError, TypeError, KeyError, OSError):
                return
        stop_owned_group("trusted_recovery_stop")


def main():
    ready_fd, nonce = int(sys.argv[1]), sys.argv[2]
    deadline = time.monotonic() + float(sys.argv[3])
    control = None
    if len(sys.argv) == 5:
        with os.fdopen(int(sys.argv[4]), "r") as specification:
            data = json.loads(specification.read(4097))
        control = RecoveryControl(data)
    response = {"type": "harness_ready", "pid": os.getpid(), "nonce": nonce}
    if control is not None:
        response["recovery_control"] = control.binding
    with os.fdopen(ready_fd, "w") as output:
        json.dump(response, output)
        output.write("\n")
        output.flush()
    # A closed supervisor pipe never starts work. The only input is a bounded JSON grant.
    grant = b""
    connected = True
    while b"\n" not in grant:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 124
        inputs = [sys.stdin.fileno()] if connected else []
        if control is not None:
            inputs.append(control.listener)
        readable, _, _ = select.select(inputs, [], [], remaining)
        if control is not None and control.listener in readable:
            control.stop_if_requested()
            continue
        if not readable:
            return 124
        part = os.read(sys.stdin.fileno(), 65536)
        if not part:
            if control is None:
                return 74
            connected = False
            continue
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
            inputs = [sys.stdin.fileno()]
            if control is not None:
                inputs.append(control.listener)
            readable, _, _ = select.select(inputs, [], [], 0.02)
            if control is not None and control.listener in readable:
                control.stop_if_requested()
            if sys.stdin.fileno() in readable and not os.read(sys.stdin.fileno(), 1):
                stop_owned_group("controller_disconnected")
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    except (KeyError, TypeError, ValueError, OSError):
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
