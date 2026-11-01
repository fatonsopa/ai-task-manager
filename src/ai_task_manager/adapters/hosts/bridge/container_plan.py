"""Finite trusted container plans and selected, read-only command snapshots."""

import math
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .workspace import Workspace


@dataclass(frozen=True)
class ContainerPlan:
    image_id: str
    argv: tuple[str, ...]
    files: tuple[str, ...]
    seconds: float = 8
    output_bytes: int = 262144
    file_bytes: int = 4194304
    workspace_mib: int = 16
    database_mib: int = 8
    temporary_mib: int = 4
    memory_mib: int = 128
    pids: int = 32
    cpus: float = 0.5

    def check(self):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id):
            raise AccessDenied("A local exact image ID is required; tags and pulls are unsupported")
        if (
            not self.argv
            or any(not isinstance(v, str) or "\0" in v for v in self.argv)
            or not self.files
            or len(self.files) > 64
            or len(set(self.files)) != len(self.files)
        ):
            raise AccessDenied("Exact argv and selected fixture files are required")
        values = (
            self.seconds,
            self.output_bytes,
            self.file_bytes,
            self.workspace_mib,
            self.database_mib,
            self.temporary_mib,
            self.memory_mib,
            self.pids,
            self.cpus,
        )
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
            for v in values
        ):
            raise AccessDenied("Container limits must be finite positive values")
        if (
            self.seconds > 8
            or self.output_bytes > 262144
            or self.file_bytes > 4194304
            or self.workspace_mib > 16
            or self.database_mib > 8
            or self.temporary_mib > 4
            or self.memory_mib > 128
            or self.pids > 32
            or self.cpus > 0.5
        ):
            raise AccessDenied("Container plan exceeds the reviewed fixture limits")


def snapshot(plan, pair, directory):
    workspace = Workspace(pair.worktree)
    root = directory / "input"
    root.mkdir(mode=0o700)
    files = {}
    total = 0
    for name in plan.files:
        path = Path(name)
        if len(name) > 256 or len(path.parts) > 8:
            raise AccessDenied("Selected fixture path exceeds the finite layout limits")
        if any(
            part.startswith(".env") or part in {".git", ".local", ".codex", ".claude", ".agents"}
            for part in path.parts
        ) or path.name in {"auth.json", ".credentials.json"}:
            raise AccessDenied("Credential and authority paths cannot enter a test snapshot")
        result = workspace.read(name)
        data = result["text"].encode()
        total += len(data)
        if total > plan.workspace_mib * 1024 * 1024 // 2:
            raise AccessDenied("Selected input exceeds the finite snapshot budget")
        target = root / path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        files[name] = result["sha256"]
    source = Path(pair.database)
    if source.is_symlink() or source.stat().st_size > plan.database_mib * 1024 * 1024 // 2:
        raise AccessDenied("Database snapshot is not a finite owned fixture")
    destination = directory / "input.sqlite"
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as before,
        sqlite3.connect(destination) as after,
    ):
        before.backup(after)
    os.chmod(destination, 0o600)
    return (
        root,
        destination,
        {"files": files, "digest": digest(files), "database_mode": "command_snapshot"},
    )
