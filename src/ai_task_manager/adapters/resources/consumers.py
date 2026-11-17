"""Bound physical resource consumers against concurrent retirement."""

import fcntl
import os
import stat
from contextlib import contextmanager

from ai_task_manager.supervisor.models import RuntimeRefused

from .paths import confined


@contextmanager
def consumer_boundary(root, attempt_id, *, exclusive=False, existing_only=False):
    """Take a nonblocking, process-shared fence; retain its inode after release."""
    directory = confined(root, "consumers")
    if not existing_only:
        directory.mkdir(mode=0o700, exist_ok=True)
    path = confined(root, f"consumers/{attempt_id}.lock")
    descriptor = None
    try:
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW
            if existing_only:
                descriptor = os.open(path, flags)
                created = False
            else:
                try:
                    descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                except FileExistsError:
                    descriptor = os.open(path, flags)
                    created = False
            opened = os.fstat(descriptor)
            current = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_size != 0
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise RuntimeRefused("Resource consumer fence ownership is uncertain")
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeRefused(
                    "An active resource consumer or cleanup retains ownership"
                ) from exc
            current = path.lstat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeRefused("Resource consumer fence identity changed")
        except OSError as exc:
            raise RuntimeRefused("Resource consumer boundary is unavailable") from exc
        yield {"device": opened.st_dev, "inode": opened.st_ino}, created
    finally:
        if descriptor is not None:
            os.close(descriptor)
