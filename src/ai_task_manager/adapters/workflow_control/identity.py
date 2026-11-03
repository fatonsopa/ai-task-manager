"""Bind local ownership to actual files, process identity and installed source."""

import hashlib
import hmac
import json
import os
import stat
import subprocess
from pathlib import Path
from uuid import UUID

from ai_task_manager.config.files import FileSeal, private_path, read_private
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError

MAX_FRAME = 262144


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def signature(key, value):
    return hmac.new(bytes.fromhex(key), canonical(value), hashlib.sha256).hexdigest()


def process_start(pid):
    if type(pid) is not int or pid < 1:
        raise AccessDenied("Invalid local owner process")
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=,lstart="], capture_output=True, text=True, timeout=2
    )
    row = result.stdout.strip().split(maxsplit=1)
    if result.returncode or len(row) != 2 or row[0] != str(os.getuid()):
        raise AccessDenied("Recorded local owner is unavailable or foreign")
    return row[1]


def source_digest():
    root = Path(__file__).resolve().parents[2]
    rows = [
        (str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*.py"))
    ]
    return hashlib.sha256(canonical(rows)).hexdigest()


def file_binding(path):
    path = private_path(path)
    value = path.stat()
    return {
        "path": str(path),
        "device": value.st_dev,
        "inode": value.st_ino,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def endpoint_binding(path):
    path = Path(path).absolute()
    if str(path.resolve()) != str(path) or path.parent.stat().st_mode & 0o077:
        raise AccessDenied("Owner endpoint requires a private physical directory")
    value = path.lstat()
    if not stat.S_ISSOCK(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o077:
        raise AccessDenied("Owner endpoint must be an owned private Unix socket")
    return {"path": str(path), "device": value.st_dev, "inode": value.st_ino}


def settings(value):
    if type(value) is not dict or set(value) != {"record", "endpoint", "instance"}:
        raise ValidationError("Unknown workflow owner configuration")
    if any(
        type(value[k]) is not str or not Path(value[k]).is_absolute()
        for k in ("record", "endpoint")
    ):
        raise ValidationError("Explicit absolute owner paths are required")
    try:
        if str(UUID(value["instance"])) != value["instance"]:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("An installed owner instance UUID is required") from exc
    if len(os.fsencode(value["endpoint"])) > 100:
        raise ValidationError("Unix endpoint path is too long")
    return dict(value)


def receive(stream):
    raw = bytearray()
    while b"\n" not in raw:
        chunk = stream.recv(min(4096, MAX_FRAME + 1 - len(raw)))
        if not chunk:
            raise AccessDenied("Incomplete local owner frame")
        raw.extend(chunk)
        if len(raw) > MAX_FRAME:
            raise AccessDenied("Local owner frame exceeds its bound")
    if raw[-1:] != b"\n" or raw.count(b"\n") != 1:
        raise AccessDenied("Unexpected local owner framing")
    from ai_task_manager.config.files import strict_json

    return strict_json(bytes(raw[:-1]))


class OwnerBinding:
    def __init__(self, config_file, scope):
        self.config_file = private_path(config_file)
        self.seal = FileSeal(config_file)
        self.config = read_private(config_file)
        self.settings = settings(self.config.get("workflow_control"))
        self.scope = scope
        self.record_seal = None

    def check(self):
        self.seal.check()
        if self.record_seal is None:
            self.record_seal = FileSeal(self.settings["record"])
        self.record_seal.check()
        row = read_private(self.settings["record"])
        if (
            type(row) is not dict
            or set(row)
            != {
                "version",
                "instance",
                "scope",
                "config",
                "source",
                "pid",
                "start",
                "endpoint",
                "key",
            }
            or type(row["version"]) is not int
            or row["version"] != 1
            or row["instance"] != self.settings["instance"]
            or row["scope"] != self.scope
            or row["config"] != file_binding(self.config_file)
            or row["source"] != source_digest()
            or row["start"] != process_start(row["pid"])
            or row["endpoint"] != endpoint_binding(self.settings["endpoint"])
            or type(row["key"]) is not str
            or len(row["key"]) != 64
        ):
            raise AccessDenied("Local owner configuration, source or scope changed")
        try:
            bytes.fromhex(row["key"])
        except ValueError as exc:
            raise AccessDenied("Invalid local authentication binding") from exc
        return row
