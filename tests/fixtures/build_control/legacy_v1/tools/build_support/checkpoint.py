"""Durable publication of a copied build snapshot, without database decisions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .values import _require


def publish_checkpoint(
    snapshot: dict[str, Any], destination: str | Path, ledger: Path
) -> dict[str, Any]:
    target = Path(destination).absolute()
    forbidden = {
        Path(str(ledger) + suffix).resolve() for suffix in ("", "-wal", "-shm", "-journal")
    }
    _require(
        target.resolve() not in forbidden and (not target.is_symlink()),
        "invalid_input",
        "Checkpoint must be a separate regular path.",
    )
    target.parent.mkdir(parents=True, exist_ok=True, mode=448)
    data = (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 384)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": str(target), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
