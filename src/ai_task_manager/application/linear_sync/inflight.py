"""Retained ownership of one bounded outbound request, never proof of an echo."""

import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.linear.errors import LinearError


def _identity(journal, scope, operation, send_id):
    path = Path(journal.path).absolute()
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise LinearError("outbound_request_journal_changed")
    value = {
        "journal": [info.st_dev, info.st_ino],
        "scope": asdict(scope),
        "operation": operation.id,
        "attempt": operation.attempt_count,
        "owner": operation.lease_owner,
        "lease_until": operation.lease_until,
        "task": operation.task_id,
        "version": operation.payload["task_version"],
        "decision": operation.payload["decision_revision"],
        "send": send_id,
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    key = hashlib.sha256(raw).hexdigest()
    return path.parent / f".linear-request-{key}.lock", raw


def _check(path, descriptor):
    current, opened = path.lstat(), os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or opened.st_uid != os.getuid()
        or opened.st_nlink != 1
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        or path.parent.is_symlink()
        or path.parent.stat().st_mode & 0o077
    ):
        raise LinearError("outbound_request_owner_changed")


@contextmanager
def owned_request(journal, scope, operation, send_id):
    path, raw = _identity(journal, scope, operation, send_id)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _check(path, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(descriptor, raw)
        os.fsync(descriptor)
        _check(path, descriptor)
        yield
        _check(path, descriptor)
    finally:
        # Keep the inode as history. An unlocked file never establishes liveness;
        # unlinking while another process probes it would break that identity.
        os.close(descriptor)


def request_in_flight(journal, scope, operation):
    """A held exact request descriptor is stronger than an unexpired DB lease."""
    send = next(
        (
            row
            for row in journal.sends(scope)
            if row["operation_id"] == operation.id
            and row["attempt"] == operation.attempt_count
            and row["state"] == "sending"
            and row["snapshot"] is None
        ),
        None,
    )
    if send is None:
        return False
    path, raw = _identity(journal, scope, operation, send["id"])
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        _check(path, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if os.read(descriptor, len(raw) + 1) != raw:
                return False
            _check(path, descriptor)
            return True
        return False
    finally:
        os.close(descriptor)
