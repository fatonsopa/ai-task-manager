"""Confined workspace I/O. Authority is checked by the caller at every short effect."""

import hashlib
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion

DENIED = frozenset({".git", ".local", ".codex", ".claude", ".agents"})
MAX_FILE = 512 * 1024


class Workspace:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise AccessDenied("An owned real workspace directory is required")
        self._identity = (self.root.stat().st_dev, self.root.stat().st_ino)

    @contextmanager
    def _parent(self, path):
        value = PurePosixPath(path)
        if (
            not isinstance(path, str)
            or not path
            or "\0" in path
            or "\\" in path
            or value.is_absolute()
            or any(p in {"..", "."} | DENIED for p in value.parts)
            or not value.parts
        ):
            raise AccessDenied("Workspace path is outside the allowed task surface")
        root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = root
        try:
            info = os.fstat(root)
            if (info.st_dev, info.st_ino) != self._identity:
                raise AccessDenied("Workspace directory identity changed")
            for part in value.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                if current != root:
                    os.close(current)
                current = child
            yield current, value.name
        except OSError as exc:
            raise AccessDenied(
                "Workspace path is inaccessible or contains a symbolic link"
            ) from exc
        finally:
            if current != root:
                os.close(current)
            os.close(root)

    def _read(self, parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AccessDenied("Workspace file must be a regular unshared file")
            data = source.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise AccessDenied("Workspace file exceeds the configured limit")
        return data, info.st_mode & 0o777

    def read(self, path):
        with self._parent(path) as (parent, name):
            data, _ = self._read(parent, name)
        return {"text": data.decode("utf-8"), "sha256": hashlib.sha256(data).hexdigest()}

    def _publish(self, parent, name, text, mode, *, create_only=False):
        temporary = ".aitm-edit-" + secrets.token_hex(12)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), mode)
                output.write(text.encode())
                output.flush()
                os.fsync(output.fileno())
            if create_only:
                try:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                            follow_symlinks=False)
                except FileExistsError as error:
                    raise StaleVersion("Workspace file already exists") from error
                os.unlink(temporary, dir_fd=parent)
            else:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass

    def create(self, path, text):
        if not isinstance(text, str) or len(text.encode()) > MAX_FILE:
            raise AccessDenied("Workspace edit exceeds the configured limit")
        with self._parent(path) as (parent, name):
            self._publish(parent, name, text, 0o644, create_only=True)
        return {"sha256": hashlib.sha256(text.encode()).hexdigest()}

    def write(self, path, text, expected_hash):
        if not isinstance(text, str) or len(text.encode()) > MAX_FILE:
            raise AccessDenied("Workspace edit exceeds the configured limit")
        with self._parent(path) as (parent, name):
            try:
                old, mode = self._read(parent, name)
            except FileNotFoundError:
                old, mode = None, 0o644
            actual = hashlib.sha256(old).hexdigest() if old is not None else None
            if expected_hash != actual:
                raise StaleVersion("Workspace file changed before this edit")
            self._publish(parent, name, text, mode)
        return {"sha256": hashlib.sha256(text.encode()).hexdigest()}
