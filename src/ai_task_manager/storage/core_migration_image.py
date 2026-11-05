"""Retain an owned Core before-image, without restore or work authority.

Automatic legacy migration supports DELETE-journal images up to 64 MiB. The
bound limits serialization memory and retained disk I/O; larger stores refuse
before capture or schema edits rather than silently skipping the before-image.
Fresh stores need no historical image. Completed migrations cannot use this
record to undo later writes, replace a file, or obtain enrollment/work authority.
"""

import hashlib
import json
import os
from pathlib import Path

from ai_task_manager.domains.work.errors import SchemaError

MAX_IMAGE_BYTES = 64 * 1024 * 1024


def _identity(path, *, private=False):
    if path.is_symlink() or not path.is_file():
        raise SchemaError("Migration image requires an owned regular file")
    value = path.stat()
    if value.st_uid != os.getuid() or (private and value.st_mode & 0o777 != 0o600):
        raise SchemaError("Migration image ownership or permissions changed")
    return [value.st_dev, value.st_ino]


def _create(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def preserve(conn, filename, version):
    """Called under the original immediate transaction, before any schema edit."""
    path = Path(filename).absolute()
    identity = _identity(path)
    if not conn.in_transaction or conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise SchemaError("Core migration requires its writable DELETE transaction")
    size = (
        conn.execute("PRAGMA page_count").fetchone()[0]
        * conn.execute("PRAGMA page_size").fetchone()[0]
    )
    if size > MAX_IMAGE_BYTES:
        raise SchemaError("Core migration before-image exceeds its supported size bound")
    image = conn.serialize()
    if len(image) > MAX_IMAGE_BYTES or _identity(path) != identity:
        raise SchemaError("Core migration source changed or exceeds its size bound")
    backup = path.with_name(path.name + f".schema{version}-before.sqlite")
    receipt = path.with_name(path.name + f".schema{version}-before.json")
    expected = {
        "version": 1,
        "source": str(path),
        "source_identity": identity,
        "source_schema": version,
        "sha256": hashlib.sha256(image).hexdigest(),
    }
    try:
        if not backup.exists() and not receipt.exists():
            _create(backup, image)
            record = dict(expected, backup_identity=_identity(backup, private=True))
            _create(receipt, (json.dumps(record, sort_keys=True) + "\n").encode())
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        backup_identity = _identity(backup, private=True)
        _identity(receipt, private=True)
        # Bound existing artifacts before reading them; incomplete capture refuses.
        if backup.stat().st_size > MAX_IMAGE_BYTES or receipt.stat().st_size > 4096:
            raise SchemaError("Core before-image evidence exceeds its supported bound")
        record = json.loads(receipt.read_text())
        if record != dict(expected, backup_identity=backup_identity):
            raise SchemaError("Core before-image is not this original migration snapshot")
        if hashlib.sha256(backup.read_bytes()).hexdigest() != expected["sha256"]:
            raise SchemaError("Core migration before-image content changed")
        if _identity(path) != identity:
            raise SchemaError("Core migration source identity changed")
    except (OSError, ValueError, TypeError) as error:
        raise SchemaError("Core migration before-image is incomplete or unavailable") from error
