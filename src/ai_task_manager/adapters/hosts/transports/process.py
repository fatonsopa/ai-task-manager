"""One owned relay and native CLI; a pipe handshake never establishes host startup."""

import json
import math
import os
import secrets
import selectors
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from ai_task_manager.adapters.process.groups import OwnedProcessGroup, require_group_monitoring
from ai_task_manager.config.files import MAX_JSON_BYTES, strict_json
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.supervisor.models import RuntimeUncertain

from ..bridge.records import ProcessIdentity
from .input_audit import InputAudit
from .native_exit import NativeExitWatch, require_exit_monitoring
from .native_input import NativeInput
from .relay_policy import command_record, prepare_binding


class HostProcess:
    def __init__(
        self,
        command,
        directory,
        *,
        deadline,
        clock=time.time,
        boundary=lambda: None,
        invocation=None,
    ):
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
        self.invocation = invocation
        self._bound_invocation = invocation
        if invocation is not None:
            from ..bridge.invocations import InitializationInvocation

            if not isinstance(invocation, InitializationInvocation):
                raise AccessDenied("A typed initialization invocation is required")
            self.deadline = min(deadline, invocation.native.binding.deadline)
        self.native_input = None
        self.input_audit = InputAudit(self.directory)
        self._active_invocation = None
        self._active_guard = None
        self._active_lease = None
        self._active_started = False
        self._model_frame = False
        self._pending_completion = None
        self._pending_submitted = False

    def start(self):
        if self.process is not None:
            raise RuntimeUncertain("Existing process must be reconciled, never relaunched")
        self.boundary()
        self._deadline()
        read_fd, write_fd = os.pipe()
        native_read, native_write = os.pipe()
        nonce = secrets.token_urlsafe(32)
        try:
            specification = {
                "nonce": nonce,
                "command": command_record(self.command),
                "policy": prepare_binding(self.invocation.native, self.command)
                if self.invocation is not None
                else None,
            }
            launch = (
                self.invocation.relay.launch_boundary()
                if self.invocation is not None
                else nullcontext(None)
            )
            with (self.directory / "native.stderr").open("xb") as err:
                os.chmod(err.name, 0o600)
                with tempfile.TemporaryFile(dir=self.directory) as specification_file:
                    os.fchmod(specification_file.fileno(), 0o600)
                    specification_file.write(json.dumps(specification, allow_nan=False).encode())
                    specification_file.flush()
                    specification_file.seek(0)
                    with launch as report:
                        self.process = subprocess.Popen(
                            [
                                sys.executable,
                                "-I",
                                str(Path(__file__).with_name("relay.py")),
                                str(write_fd),
                                nonce,
                                str(self.deadline - self.clock()),
                                str(self.deadline),
                                str(specification_file.fileno()),
                                str(native_read),
                            ],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=err,
                            env={"PATH": os.defpath},
                            cwd=self.directory,
                            pass_fds=(write_fd, specification_file.fileno(), native_read),
                            start_new_session=True,
                        )
                        if report is not None:
                            report(process_pid=None, process_start_identity=None)
            os.close(native_read)
            native_read = -1
            self.native_input = NativeInput(native_write, deadline=self.deadline, clock=self.clock)
            native_write = -1
            os.set_blocking(self.process.stdin.fileno(), False)
            os.close(write_fd)
            write_fd = -1
            self.group = OwnedProcessGroup(self.process)
            if not self.group.capture().known:
                raise RuntimeUncertain("Owned relay identity is not observable")
            self._control(
                {
                    "op": "launch",
                    "nonce": nonce,
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
        except BaseException as error:
            try:
                if self.group is None and self.process is not None:
                    self.group = OwnedProcessGroup(self.process)
                    self.group.capture()
                if self.group is not None:
                    error.owned_stop_evidence = self.stop()
            except BaseException as cleanup_error:
                error.owned_stop_error = type(cleanup_error).__name__
            raise
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
            if native_read >= 0:
                os.close(native_read)
            if native_write >= 0:
                os.close(native_write)

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
        if len(raw) > min(MAX_JSON_BYTES, os.fpathconf(self.process.stdin.fileno(), "PC_PIPE_BUF")):
            raise AccessDenied("Host input exceeds the frame limit")
        if os.write(self.process.stdin.fileno(), raw) != len(raw):
            raise RuntimeUncertain("Relay control commit is partial or unknown")

    @contextmanager
    def use_invocation(self, permit, guard=None, lease=None):
        from ..bridge.invocations import PreparedInvocation

        if (
            not isinstance(permit, PreparedInvocation)
            or self.invocation is None
            or self._active_invocation is not None
            or self._pending_completion is not None
        ):
            raise AccessDenied("A separate registered invocation is required")
        self._check_invocation_owner(permit)
        native = self.invocation.native.binding
        snapshot = permit.issuer.controller.snapshot()
        actual = next(row for row in snapshot["calls"] if row["call_id"] == permit.binding.call_id)
        if actual["metadata"].get("parent_call_id") != native.call_id:
            raise AccessDenied("Submission is not registered under this native host")
        self._pending_completion, self._pending_submitted = permit, False
        self._control({"op": "invocation", "call_id": permit.binding.call_id})
        self._active_invocation, self._active_guard, self._active_lease = permit, guard, lease
        self._active_started = permit.mode == "running"
        try:
            yield
        finally:
            self._active_invocation = self._active_guard = None
            self._active_lease = None
            self._active_started = False

    def _check_invocation_owner(self, permit):
        if self.invocation is None or self.invocation is not self._bound_invocation:
            raise AccessDenied("Original native invocation ownership changed")
        native = self.invocation.native.binding
        if (
            permit.issuer.controller is not self.invocation.native.issuer.controller
            or permit.binding.purpose not in {"neutral", "assignment"}
        ):
            raise AccessDenied("Submission authority differs from its native host")
        if any(
            getattr(permit.binding, name) != getattr(native, name)
            for name in (
                "run_id",
                "policy_revision",
                "worker_identity",
                "attempt_id",
                "host",
                "configuration_digest",
                "model",
                "effort",
            )
        ):
            raise AccessDenied("Submission invocation differs from its owned native host")

    @contextmanager
    def _input_boundary(self):
        permit = self._active_invocation
        if self.invocation is None:
            yield
            return
        if permit is None or (not self._active_started and not self._model_frame):
            permit = self.invocation.native.running()
        prepared_guard = self._active_guard
        root = (
            permit.effect_boundary()
            if self._active_started or permit.mode == "running"
            else permit.launch_boundary()
        )
        with root as report:
            guard = (
                prepared_guard.owner.current_boundary(prepared_guard)
                if prepared_guard is not None
                else nullcontext()
            )
            with guard:
                self.check_current_liveness()
                lease = self._active_lease() if self._active_lease is not None else nullcontext()
                with lease:
                    yield
                    if self._model_frame:
                        self._pending_submitted = True
                if report is not None:
                    report(process_pid=None, process_start_identity=None)
                    self._active_started = True

    def complete_invocation(self, permit):
        if (
            self.invocation is None
            or self._pending_completion is not permit
            or not self._pending_submitted
            or self._active_invocation is not None
            or self.clock() >= permit.binding.deadline
            or permit.issuer.controller is not self.invocation.native.issuer.controller
        ):
            raise AccessDenied("Invocation completion lacks its exact pending submission")
        self._check_invocation_owner(permit)
        permit.issuer._validate(permit)
        snapshot = permit.issuer.controller.snapshot()
        call = next((r for r in snapshot["calls"] if r["call_id"] == permit.binding.call_id), None)
        from ..bridge.invocations import InvocationBinding

        if (
            call is None
            or call["status"] != "succeeded"
            or call["outcome"] != "succeeded"
            or call["process_stopped"] is not True
            or call["uses_worker"]
            or call["metadata"].get("parent_call_id") != self.invocation.native.binding.call_id
            or InvocationBinding.observed(
                {
                    "run_id": snapshot["run_id"],
                    "policy_revision": snapshot["policy"]["revision"],
                    "call": call,
                }
            )
            != permit.binding
        ):
            raise AccessDenied("Exact independently recorded invocation completion is required")
        self._control({"op": "complete", "call_id": permit.binding.call_id})
        self._pending_completion, self._pending_submitted = None, False

    def write(self, text):
        self._deadline()
        self.boundary()
        self.verify_identity()
        if self.invocation is not None and self._active_invocation is None:
            # Host initialization protocol is permitted, model submission is not.
            for frame in NativeInput.frames(text):
                data = strict_json(frame)
                if data.get("type") == "user" or data.get("method") == "turn/start":
                    raise AccessDenied("Model submission requires its own invocation")
        try:
            frames = [strict_json(frame) for frame in NativeInput.frames(text)]
            model_frames = [
                frame.get("type") == "user" or frame.get("method") == "turn/start"
                for frame in frames
            ]
            if len(set(model_frames)) > 1:
                raise AccessDenied("Model and control frames need separate exact submissions")
            self._model_frame = bool(model_frames and model_frames[0])
            self.native_input.send(
                text,
                boundary=self._input_boundary,
                invocation_deadline=self._active_invocation.binding.deadline
                if self._active_invocation is not None
                else None,
                observe=lambda outcome: self.input_audit.record(
                    outcome,
                    self._active_invocation.binding.call_id
                    if self._active_invocation is not None
                    else self.invocation.native.binding.call_id
                    if self.invocation is not None
                    else None,
                ),
            )
        except BaseException as error:
            if getattr(error, "native_input_uncertain", False):
                try:
                    error.owned_stop_evidence = self.stop()
                except BaseException as cleanup_error:
                    error.owned_stop_error = type(cleanup_error).__name__
            raise
        finally:
            self._model_frame = False

    def end_input(self):
        self.native_input.close()

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
            if self.native_input is not None:
                self.native_input.close()
            self._closed = True
            try:
                if self.native_exit is not None:
                    self.native_exit.close()
            finally:
                self.group.reap_exited_leader()
                if self.process.stdout:
                    self.process.stdout.close()
        return result
