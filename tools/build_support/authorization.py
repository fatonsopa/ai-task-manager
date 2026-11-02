"""Resolve explicitly configured, scoped records; hashes bind content, not user origin."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .values import ControlError, _require


@dataclass(frozen=True)
class Record:
    kind: str
    run_id: str
    sha256: str
    data: dict[str, Any]


class RecordVerifier(Protocol):
    def resolve(self, ref: str, kind: str, run_id: str) -> Record: ...


class NoRecords:
    def resolve(self, ref: str, kind: str, run_id: str) -> Record:
        raise ControlError("authorization_required", "No record authority is configured.")


def read_record(path: Path, expected_sha256: str) -> dict[str, Any]:
    _require(
        path.is_file() and not path.is_symlink(),
        "invalid_authorization",
        "A regular pinned record is required.",
    )
    data = path.read_bytes()
    _require(
        hashlib.sha256(data).hexdigest() == expected_sha256,
        "invalid_authorization",
        "The configured record bytes changed.",
    )
    try:
        value = json.loads(data)
        json.dumps(value, allow_nan=False)
    except (ValueError, UnicodeError) as error:
        raise ControlError("invalid_authorization", "Invalid configured record.") from error
    _require(isinstance(value, dict), "invalid_authorization", "Records must be objects.")
    return value


class PinnedRecords:
    """Trusted construction pins each ref to a path/hash/kind/run; no public proof mint.

    The lead reviews the exact config and native-user source. This is content and
    scope verification, not cryptographic proof of a message or a same-user sandbox.
    """

    def __init__(self, bindings: dict[str, dict[str, str]]):
        _require(
            isinstance(bindings, dict)
            and all(
                isinstance(ref, str)
                and isinstance(binding, dict)
                and set(binding) == {"kind", "run_id", "path", "sha256"}
                and all(isinstance(v, str) and v for v in binding.values())
                for ref, binding in bindings.items()
            ),
            "invalid_authorization",
            "Record bindings need their exact declared fields.",
        )
        self.bindings = {ref: dict(binding) for ref, binding in bindings.items()}

    @classmethod
    def from_config(cls, path: str | Path, expected_sha256: str) -> PinnedRecords:
        value = read_record(Path(path), expected_sha256)
        _require(
            value.get("format") == 1 and isinstance(value.get("bindings"), dict),
            "invalid_authorization",
            "Unsupported record authority configuration.",
        )
        return cls(value["bindings"])

    def resolve(self, ref: str, kind: str, run_id: str) -> Record:
        _require(
            isinstance(ref, str),
            "authorization_required",
            "A configured record reference is required.",
        )
        binding = self.bindings.get(ref)
        _require(
            isinstance(binding, dict)
            and binding.get("kind") == kind
            and binding.get("run_id") == run_id,
            "authorization_required",
            "The record is not configured for this purpose/run.",
        )
        path, sha = binding.get("path"), binding.get("sha256")
        _require(
            isinstance(path, str) and isinstance(sha, str),
            "invalid_authorization",
            "Incomplete record binding.",
        )
        _require(
            ref == f"{kind}:{sha}",
            "invalid_authorization",
            "Record references must bind their purpose and exact content hash.",
        )
        return Record(kind, run_id, sha, read_record(Path(path), sha))
