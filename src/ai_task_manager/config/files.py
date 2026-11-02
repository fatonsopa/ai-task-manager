"""Bounded private JSON files; credentials are never shell input."""

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied, ValidationError

MAX_JSON_BYTES = 262144


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def strict_json(raw):
    if len(raw.encode() if isinstance(raw, str) else raw) > MAX_JSON_BYTES:
        raise ValidationError("Input exceeds the configured size limit")

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValidationError("Duplicate JSON object key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValidationError("Nonfinite JSON value")),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Input must be valid UTF-8 JSON") from exc


def private_path(path, *, existing=True):
    path = Path(path).absolute()
    if path.is_symlink() or path.parent.is_symlink():
        raise AccessDenied("Private state cannot use symbolic links")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise AccessDenied("Private file must be owned by this user with mode 600")
    elif existing:
        raise AccessDenied("Required private file is unavailable")
    return path


def read_private(path):
    path = private_path(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    return strict_json(raw)


def write_private(path, value, *, replace=False):
    path = private_path(path, existing=False)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise AccessDenied("Private state directory must have mode 700")
    data = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if not replace:
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class FileSeal:
    def __init__(self, path):
        self.path = private_path(path)
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def check(self):
        private_path(self.path)
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.sha256:
            raise AccessDenied("Configuration changed; restart and verify the new binding")

    def metadata(self):
        """Inspect the sealed private file without reading its contents."""
        try:
            info = private_path(self.path).lstat()
            return (
                info.st_dev,
                info.st_ino,
                info.st_uid,
                info.st_mode,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        except OSError:
            raise AccessDenied("Sealed configuration metadata is unavailable") from None
