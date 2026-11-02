"""One owned relay and native CLI; a pipe handshake never establishes host startup."""

import json
import math
import os
import secrets
import selectors
import subprocess
import sys
import time
from pathlib import Path

from ai_task_manager.adapters.process.groups import OwnedProcessGroup, require_group_monitoring
from ai_task_manager.config.files import MAX_JSON_BYTES, strict_json
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.supervisor.models import RuntimeUncertain

from ..bridge.records import ProcessIdentity
from .native_exit import NativeExitWatch, require_exit_monitoring


class HostProcess:
    def __init__(self, command, directory, *, deadline, clock=time.time, boundary=lambda: None):
        if (
            isinstance(deadline, bool)
            or not math.isfinite(deadline)
            or deadline <= clock()
            or not command.argv
            or not Path(command.argv[0]).is_absolute()
            or not Path(command.cwd).is_dir()
        ):
            raise AccessDenied(
                "An exact executable, work directory and future deadline are required"
            )
        require_group_monitoring()
        require_exit_monitoring()
        self.command, self.deadline, self.clock, self.boundary = command, deadline, clock, boundary
        self.directory = Path(directory).absolute()
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise AccessDenied("Host output needs an owned private directory")
        self._buffer, self._closed = b"", False
        self.process = self.group = self.identity = self.native_exit = None

    def start(self):
        if self.process is not None:
            raise RuntimeUncertain("Existing process must be reconciled, never relaunched")
        self.boundary()
        self._deadline()
        read_fd, write_fd = os.pipe()
        nonce = secrets.token_urlsafe(32)
        try:
            with (self.directory / "native.stderr").open("xb") as err:
                os.chmod(err.name, 0o600)
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("relay.py")),
                        str(write_fd),
                        nonce,
                        str(self.deadline - self.clock()),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=err,
                    env={"PATH": os.defpath},
                    cwd=self.directory,
                    pass_fds=(write_fd,),
                    start_new_session=True,
                )
            os.close(write_fd)
            write_fd = -1
            self.group = OwnedProcessGroup(self.process)
            if not self.group.capture().known:
                raise RuntimeUncertain("Owned relay identity is not observable")
            self._control(
                {
                    "op": "launch",
                    "nonce": nonce,
                    "argv": self.command.argv,
                    "cwd": str(self.command.cwd),
                    "environment": self.command.environment,
                }
            )
            with selectors.DefaultSelector() as selected:
                selected.register(read_fd, selectors.EVENT_READ)
                if not selected.select(min(3, self.deadline - self.clock())):
                    raise RuntimeUncertain("Native process identity handshake timed out")
                raw = os.read(read_fd, 4097)
            record = strict_json(raw)
            observed = self.group.observe()
            native = next((p for p in observed.members if p.pid == record.get("pid")), None)
            if (
                record.get("nonce") != nonce
                or not observed.known
                or native is None
                or not native.live
                or native.pid == self.process.pid
            ):
                raise RuntimeUncertain("Actual native process ownership is not established")
            self.identity = ProcessIdentity(native.pid, native.start_identity, self.group.group_id)
            self.native_exit = NativeExitWatch.capture(self)
            return self.identity
        except BaseException:
            if self.group is not None:
                self.stop()
            raise
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def _deadline(self):
        if self.clock() >= self.deadline:
            raise AccessDenied("Original managed-host deadline expired")

    def verify_identity(self):
        self._deadline()
        if self.identity is None or self._closed:
            raise AccessDenied("Native process is not active")
        observation = self.group.observe()
        if not observation.known or not any(
            p.pid == self.identity.pid
            and p.start_identity == self.identity.start_identity
            and p.live
            for p in observation.members
        ):
            raise RuntimeUncertain("Native process identity is unknown or no longer active")
        return self.identity

    def check_current_liveness(self):
        self._deadline()
        if self._closed or self.native_exit is None or self.process is None:
            raise RuntimeUncertain("Native process lacks its original kernel exit handle")
        self.native_exit.check_current(self.identity, self.process.pid, self.group.start_identity)

    def _control(self, data):
        if self._closed or self.process is None or self.process.stdin is None:
            raise AccessDenied("Host transport is closed")
        raw = (json.dumps(data, allow_nan=False) + "\n").encode()
        if len(raw) > MAX_JSON_BYTES:
            raise AccessDenied("Host input exceeds the frame limit")
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def write(self, text):
        self._deadline()
        self.boundary()
        self.verify_identity()
        self._control({"op": "write", "data": text})

    def end_input(self):
        self._control({"op": "eof"})

    def read(self, timeout=0.2):
        self._deadline()
        self.boundary()
        end = time.monotonic() + timeout
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                try:
                    value = strict_json(line)
                except ValidationError as error:
                    raise AccessDenied("Native host emitted an invalid frame") from error
                if not isinstance(value, dict):
                    raise AccessDenied("Host records must be JSON objects")
                return value
            if len(self._buffer) > MAX_JSON_BYTES:
                raise AccessDenied("Host frame exceeds the configured limit")
            remaining = min(end - time.monotonic(), self.deadline - self.clock())
            if remaining <= 0:
                return None
            with selectors.DefaultSelector() as selected:
                selected.register(self.process.stdout, selectors.EVENT_READ)
                if not selected.select(remaining):
                    return None
                part = os.read(self.process.stdout.fileno(), 65536)
            if not part:
                if self._buffer:
                    raise RuntimeUncertain("Host ended with an incomplete frame")
                raise RuntimeUncertain("Host transport disconnected")
            self._buffer += part

    def stop(self):
        if self._closed:
            return self.stop_evidence
        if self.group is None:
            raise RuntimeUncertain("A process without an ownership anchor cannot be released")
        result = self.group.stop()
        self.stop_evidence = result
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if result.confirmed:
            self._closed = True
            try:
                if self.native_exit is not None:
                    self.native_exit.close()
            finally:
                self.group.reap_exited_leader()
                if self.process.stdout:
                    self.process.stdout.close()
        return result
