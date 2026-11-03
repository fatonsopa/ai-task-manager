"""Read only explicitly installed captured judgments; integrity is not authenticity.

The trusted installer must establish the observation's real origin. This reader
cannot infer genuine human/model review from an owned file, trace or signature.
"""

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ai_task_manager.config.files import MAX_JSON_BYTES, private_path, strict_json
from ai_task_manager.domains.work.errors import EvidenceRequired, IdempotencyConflict
from ai_task_manager.domains.work.models import Scope

from .models import CapturedReview, FileWitness


@dataclass(frozen=True)
class ReviewBinding:
    reference: str
    context_digest: str
    scope: Scope
    task_id: str
    operation_key: str
    capture_path: Path
    capture_sha256: str
    trace_path: Path
    trace_sha256: str
    reviewer_id: str
    host: str
    model: str | None
    provenance_ref: str


class MissingReviews:
    def resolve(self, inspection):
        return None


class InstalledReviews:
    def __init__(self, key_path, revocation_path, bindings=(), *, selected=None):
        self.key_path = private_path(key_path)
        self.revocation_path = private_path(revocation_path)
        self._key = self.key_path.read_bytes()
        if len(self._key) != 32 or self.key_path.stat().st_nlink != 1:
            raise EvidenceRequired("An installed private capture-verification key is required")
        self.key_witness = FileWitness.capture(self.key_path)
        owned, keys = {}, {}
        for binding in bindings:
            if type(binding) is not ReviewBinding or not all(
                (
                    binding.reference,
                    binding.reviewer_id,
                    binding.host,
                    binding.operation_key,
                    binding.provenance_ref,
                )
            ):
                raise EvidenceRequired("Explicit installed review provenance is required")
            if any(
                len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                for value in (binding.context_digest, binding.capture_sha256, binding.trace_sha256)
            ):
                raise EvidenceRequired("Installed source and context hashes are required")
            key = (binding.scope, binding.task_id, binding.operation_key)
            if key in keys and keys[key] != binding:
                raise IdempotencyConflict("A captured review key has conflicting immutable content")
            if binding.reference in owned:
                raise IdempotencyConflict("A review reference cannot have two installed sources")
            owned[binding.reference], keys[key] = binding, binding
        self.bindings = MappingProxyType(owned)
        self.selected = MappingProxyType(dict(selected or {}))
        self._bound = (self.bindings, self.selected, self.key_path, self.revocation_path)
        if any(
            ref not in owned or owned[ref].context_digest != context
            for context, ref in self.selected.items()
        ):
            raise EvidenceRequired("Selected review is not installed for its exact context")

    def check_binding(self):
        current = (self.bindings, self.selected, self.key_path, self.revocation_path)
        if any(a is not b for a, b in zip(current, self._bound)):
            raise EvidenceRequired("The installed review provenance binding changed")

    @staticmethod
    def _read(path, expected=None):
        path = private_path(path)
        if path.stat().st_nlink != 1:
            raise EvidenceRequired("Installed captured sources must be unshared files")
        witness = FileWitness.capture(path)
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        witness.check()
        if len(raw) > MAX_JSON_BYTES or (expected and hashlib.sha256(raw).hexdigest() != expected):
            raise EvidenceRequired("Installed review source content changed or exceeds its bound")
        return raw, witness

    def resolve(self, inspection):
        self.check_binding()
        reference = self.selected.get(inspection.digest)
        if reference is None:
            return None
        binding = self.bindings[reference]
        context = inspection.context
        from dataclasses import asdict

        if context["scope"] != asdict(binding.scope) or context["task_id"] != binding.task_id:
            raise EvidenceRequired("Installed review belongs to another task or scope")
        self.key_witness.check()
        revocations, revoked_witness = self._read(self.revocation_path)
        revoked = strict_json(revocations)
        if (
            not isinstance(revoked, dict)
            or set(revoked) != {"revoked"}
            or not isinstance(revoked["revoked"], list)
        ):
            raise EvidenceRequired("Installed revocation state is unavailable")
        if reference in revoked["revoked"]:
            raise EvidenceRequired("Captured review was revoked")
        raw, capture = self._read(binding.capture_path, binding.capture_sha256)
        value = strict_json(raw)
        if not isinstance(value, dict) or set(value) != {"record", "signature"}:
            raise EvidenceRequired("Captured review envelope is invalid")
        from ai_task_manager.domains.checks.models import canonical

        record = value["record"]
        expected = hmac.new(self._key, canonical(record).encode(), hashlib.sha256).hexdigest()
        if not isinstance(value["signature"], str) or not hmac.compare_digest(
            expected, value["signature"]
        ):
            raise EvidenceRequired("Captured review signature is invalid")
        fields = {
            "reference",
            "context_digest",
            "operation_key",
            "reviewer_id",
            "host",
            "model",
            "provenance_ref",
            "trace_sha256",
            "criteria",
            "reason",
            "expires_at",
        }
        if not isinstance(record, dict) or set(record) != fields:
            raise EvidenceRequired("Captured review fields are incomplete")
        if any(
            record[name] != getattr(binding, name)
            for name in fields - {"criteria", "reason", "expires_at"}
        ):
            raise EvidenceRequired("Capture differs from installed observation provenance")
        trace, trace_witness = self._read(binding.trace_path, binding.trace_sha256)
        if not trace.strip():
            raise EvidenceRequired("Actual captured public review trace is required")
        trace.decode("utf-8")
        criteria = record["criteria"]
        if (
            not isinstance(criteria, list)
            or not criteria
            or any(
                not isinstance(c, dict)
                or set(c) != {"id", "judgment", "reason"}
                or any(not isinstance(v, str) for v in c.values())
                for c in criteria
            )
            or not isinstance(record["reason"], str)
            or type(record["expires_at"]) not in {float, int}
        ):
            raise EvidenceRequired(
                "Actual criteria, reasons and finite review lifetime are required"
            )
        return CapturedReview(
            reference,
            inspection.digest,
            binding.reviewer_id,
            binding.host,
            binding.model,
            binding.provenance_ref,
            binding.trace_sha256,
            binding.capture_sha256,
            tuple((c["id"], c["judgment"], c["reason"]) for c in criteria),
            record["reason"],
            record["expires_at"],
            (self.key_witness, revoked_witness, capture, trace_witness),
        )
