"""Parent-owned native input: incomplete JSON first, bounded final commit last."""

import hashlib
import json
import os
import selectors
import threading
import time
from dataclasses import dataclass, replace

from ai_task_manager.config.files import MAX_JSON_BYTES, strict_json
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError


@dataclass(frozen=True)
class InputOutcome:
    content_sha256: str
    frame_count: int
    prefix_bytes: int
    suffix_bytes: int
    written_frames: int
    confirmed_boundaries: int
    uncertain: bool
    phase: str
    error_type: str | None


class NativeInput:
    def __init__(self, fd, *, deadline, clock=time.time):
        self.fd, self.deadline, self.clock = fd, deadline, clock
        self.quarantined = False
        self.closed = False
        self.lock = threading.Lock()
        self.last_outcome = None
        os.set_blocking(fd, False)
        if os.fpathconf(fd, "PC_PIPE_BUF") < 2:
            raise AccessDenied("Native input cannot provide a bounded final frame commit")

    @staticmethod
    def frames(text):
        if not isinstance(text, str) or not text or len(text.encode()) > MAX_JSON_BYTES:
            raise AccessDenied("Native input exceeds its declared frame bound")
        result = []
        try:
            lines = text.split("\n")
            if lines[-1] == "":
                lines.pop()
            for line in lines:
                if not line:
                    raise AccessDenied("Empty native protocol frame")
                value = strict_json(line.encode())
                if not isinstance(value, dict):
                    raise AccessDenied("Native protocol input must be one JSON object")
                encoded = json.dumps(
                    value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode()
                if not encoded.endswith(b"}") or len(encoded) + 1 > MAX_JSON_BYTES:
                    raise AccessDenied("Native protocol object is not bounded")
                result.append(encoded)
        except (ValidationError, ValueError, UnicodeError) as error:
            raise AccessDenied("Native protocol framing is unsupported") from error
        if sum(len(frame) + 1 for frame in result) > MAX_JSON_BYTES:
            raise AccessDenied("Native input exceeds its aggregate frame bound")
        return result

    def _stage(self, data, deadline):
        remaining = memoryview(data)
        try:
            with selectors.DefaultSelector() as selected:
                selected.register(self.fd, selectors.EVENT_WRITE)
                while remaining:
                    if self.clock() >= deadline:
                        raise AccessDenied("Native input staging deadline expired")
                    try:
                        written = os.write(self.fd, remaining)
                        if written <= 0:
                            raise AccessDenied("Native input staging is unresolved")
                        remaining = remaining[written:]
                    except BlockingIOError:
                        selected.select(min(0.05, max(0, deadline - self.clock())))
        except BaseException as error:
            error.native_staged_bytes = len(data) - len(remaining)
            raise
        return len(data)

    def send(self, text, *, boundary, invocation_deadline=None, observe=None):
        frames = self.frames(text)
        deadline = min(
            self.deadline,
            invocation_deadline if invocation_deadline is not None else self.deadline,
        )
        with self.lock:
            if self.quarantined or self.closed:
                raise AccessDenied("Native input is quarantined or closed; never resend")
            staged = False
            prefix = suffix = written_frames = confirmed = 0
            phase, primary = "stage", None
            try:
                for frame in frames:
                    # The complete object cannot be parsed even by an EOF parser.
                    staged = True
                    phase = "stage"
                    try:
                        prefix += self._stage(frame[:-1], deadline)
                    except BaseException as error:
                        prefix += getattr(error, "native_staged_bytes", 0)
                        raise
                    phase = "commit"
                    with boundary():
                        if self.clock() >= deadline:
                            raise AccessDenied("Native input commit deadline expired")
                        count = os.write(self.fd, b"}\n")
                        suffix += count
                        if count != 2:
                            raise AccessDenied("Native input commit is partial or unknown")
                        written_frames += 1
                        phase = "boundary_exit"
                    confirmed += 1
                    staged = False
                phase = "completed"
            except BaseException as error:
                primary = error
                if staged:
                    self.quarantined = True
                    error.native_input_uncertain = True
                raise
            finally:
                outcome = InputOutcome(
                    hashlib.sha256(b"\n".join(frames)).hexdigest(),
                    len(frames),
                    prefix,
                    suffix,
                    written_frames,
                    confirmed,
                    staged,
                    phase,
                    type(primary).__name__ if primary is not None else None,
                )
                self.last_outcome = outcome
                if observe is not None:
                    # The serialization lock still excludes later dependent
                    # input. Root/model/Core contexts have already unwound.
                    try:
                        observe(outcome)
                    except BaseException as audit_error:
                        self.quarantined = True
                        self.last_outcome = replace(outcome, uncertain=True, phase="audit")
                        if primary is not None:
                            primary.input_audit_error = type(audit_error).__name__
                        else:
                            audit_error.native_input_uncertain = True
                            raise
            return self.last_outcome

    def close(self):
        if not self.closed:
            os.close(self.fd)
            self.closed = True
