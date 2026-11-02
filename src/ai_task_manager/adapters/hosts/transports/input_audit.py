"""Bounded transport observations; these records never grant task authority."""

import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied

from .native_input import InputOutcome

MAX_AUDIT_BYTES = 1048576
MAX_RECORD_BYTES = 4096


class InputAudit:
    def __init__(self, directory):
        self.path = Path(directory) / "native-input.jsonl"
        self.failed = False

    def record(self, outcome, call_id):
        if not isinstance(outcome, InputOutcome):
            raise AccessDenied("Input audit needs its typed transport observation")
        if self.failed:
            raise AccessDenied("Required input audit remains unavailable")
        raw = (
            json.dumps(
                {"call_id": call_id, "outcome": asdict(outcome)}, sort_keys=True, allow_nan=False
            )
            + "\n"
        ).encode()
        if len(raw) > MAX_RECORD_BYTES:
            self.failed = True
            raise AccessDenied("Input observation exceeds its fixed record limit")
        fd = None
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size + len(raw) > MAX_AUDIT_BYTES
            ):
                raise AccessDenied("Required input audit is unsafe or full")
            if os.write(fd, raw) != len(raw):
                raise AccessDenied("Required input audit write is partial")
            os.fsync(fd)
        except BaseException:
            self.failed = True
            raise
        finally:
            if fd is not None:
                os.close(fd)
