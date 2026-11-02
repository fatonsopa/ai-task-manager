"""Runtime verification of explicitly installed evaluation records.

The installer owns source provenance. A content hash cannot establish user
authorization, and a record is not a native, provider or human observation.
"""

import hashlib
from dataclasses import dataclass
from types import MappingProxyType

from ai_task_manager.domains.work.errors import AccessDenied

from .files import MAX_JSON_BYTES, private_path, strict_json

PURPOSES = frozenset({"native_bootstrap_review", "evaluation_grant", "evaluation_dependencies"})


@dataclass(frozen=True)
class InstalledEvaluationRecord:
    kind: str
    run_id: str
    sha256: str
    data: dict


class EvaluationRecords:
    def __init__(self, bindings):
        if not isinstance(bindings, dict) or not bindings:
            raise AccessDenied("Explicit installed evaluation record bindings are required")
        owned = {}
        for ref, binding in bindings.items():
            if (
                not isinstance(ref, str)
                or not isinstance(binding, dict)
                or set(binding) != {"kind", "run_id", "path", "sha256"}
                or binding["kind"] not in PURPOSES
                or any(not isinstance(value, str) or not value for value in binding.values())
                or len(binding["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in binding["sha256"])
                or ref != binding["kind"] + ":" + binding["sha256"]
            ):
                raise AccessDenied("Unknown or incomplete evaluation record purpose/binding")
            owned[ref] = MappingProxyType(dict(binding))
        self.bindings = self._original_bindings = MappingProxyType(owned)

    def resolve(self, ref, kind, run_id):
        if self.bindings is not self._original_bindings or kind not in PURPOSES:
            raise AccessDenied("The installed evaluation record authority changed")
        binding = self.bindings.get(ref) if isinstance(ref, str) else None
        if binding is None or (binding["kind"], binding["run_id"]) != (kind, run_id):
            raise AccessDenied("Record is not installed for this exact run and purpose")
        path = private_path(binding["path"])
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES or hashlib.sha256(raw).hexdigest() != binding["sha256"]:
            raise AccessDenied("The installed evaluation record changed or exceeded its bound")
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise AccessDenied("The installed evaluation record must be an object")
        return InstalledEvaluationRecord(kind, run_id, binding["sha256"], value)
