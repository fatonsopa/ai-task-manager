"""Persist only captures produced by the actual independent native reviewer."""

import hashlib
import hmac
import json
import os
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.config.files import MAX_JSON_BYTES, private_path, read_private, write_private
from ai_task_manager.domains.checks.models import canonical
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, ValidationError
from ai_task_manager.domains.work.models import Scope

from .models import FileWitness
from .source import InstalledReviews, ReviewBinding


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CaptureStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.resolve() != self.root or self.root.stat().st_mode & 0o077:
            raise EvidenceRequired("Review captures require original private storage")
        self._root_identity = self._identity()
        self.key = self.root / "capture.key"
        self.revoked = self.root / "revoked.json"
        # Any existing state makes this a reopen, including interrupted setup.
        # Missing authority files require explicit recovery, never regeneration.
        if not any(self.root.iterdir()):
            with self.key.open("xb") as stream:
                self.key.chmod(0o600)
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            write_private(self.revoked, {"revoked": []})
        try:
            self.key_witness = FileWitness.capture(private_path(self.key))
            if len(self.key.read_bytes()) != 32 or self.key.stat().st_nlink != 1:
                raise EvidenceRequired("Original review capture key is unavailable")
            self._check_revocations()
        except (OSError, AccessDenied, ValidationError) as error:
            raise EvidenceRequired("Original review capture authority is unavailable") from error

    def _check_revocations(self):
        try:
            value = read_private(self.revoked)
        except (OSError, AccessDenied, ValidationError) as error:
            raise EvidenceRequired("Original review revocation state is unavailable") from error
        if (
            type(value) is not dict
            or set(value) != {"revoked"}
            or type(value["revoked"]) is not list
            or any(type(ref) is not str or not ref for ref in value["revoked"])
        ):
            raise EvidenceRequired("Original review revocation state is invalid")

    @staticmethod
    def _serialized(value):
        # Match write_private exactly, including indentation, escaping and LF.
        raw = (
            json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode()
        if len(raw) > MAX_JSON_BYTES:
            raise EvidenceRequired("Serialized review exceeds the installed reader bound")
        return raw

    def _identity(self):
        info = self.root.stat()
        return (info.st_dev, info.st_ino, info.st_uid, info.st_mode)

    def check_binding(self):
        if self.root.resolve() != self.root or self._identity() != self._root_identity:
            raise EvidenceRequired("Original review capture directory changed")
        self.key_witness.check()
        # Core consumption checks the revocation witness prepared by resolve,
        # through WorkflowReviews; it must not reopen source content here.

    def directory(self, inspection):
        self.check_binding()
        return self.root / inspection.digest

    def require_unused(self, inspection):
        if (self.directory(inspection) / "intent.json").exists():
            raise EvidenceRequired("Prior review intent requires reconciliation; never relaunch")

    def begin(self, inspection, registration):
        directory = self.directory(inspection)
        directory.mkdir(mode=0o700, exist_ok=True)
        intent = directory / "intent.json"
        if intent.exists():
            raise EvidenceRequired("Prior review intent requires reconciliation; never relaunch")
        write_private(intent, {"context_digest": inspection.digest, "registration": registration})
        return directory

    def failed(self, directory, error, *, outcome="failed_or_unknown"):
        path = directory / "failure.json"
        if not path.exists():
            write_private(
                path,
                {
                    "type": type(error).__name__,
                    "message": str(error),
                    "outcome": outcome,
                },
            )

    def _publish(self, inspection, completed):
        """Only the live producer's opaque completion can publish a judgment."""
        from .native import CompletedNativeReview

        if type(completed) is not CompletedNativeReview:
            raise EvidenceRequired("An actual independent native review completion is required")
        record, trace = completed.export(inspection)
        self.check_binding()
        inspection.check()
        directory = self.directory(inspection)
        trace_path, capture_path = directory / "public-trace.json", directory / "capture.json"
        trace_bytes = self._serialized(trace)
        record = {**record, "trace_sha256": hashlib.sha256(trace_bytes).hexdigest()}
        signature = hmac.new(
            self.key.read_bytes(), canonical(record).encode(), hashlib.sha256
        ).hexdigest()
        envelope = {"record": record, "signature": signature}
        capture_bytes = self._serialized(envelope)
        binding = ReviewBinding(
            record["reference"],
            inspection.digest,
            Scope(**inspection.context["scope"]),
            inspection.context["task_id"],
            record["operation_key"],
            capture_path,
            hashlib.sha256(capture_bytes).hexdigest(),
            trace_path,
            hashlib.sha256(trace_bytes).hexdigest(),
            record["reviewer_id"],
            record["host"],
            record["model"],
            record["provenance_ref"],
        )
        value = asdict(binding)
        value.update(capture_path=str(capture_path), trace_path=str(trace_path))
        self._serialized(value)
        # All immutable reader inputs fit before publishing any of them.
        write_private(trace_path, trace)
        write_private(capture_path, envelope)
        write_private(directory / "binding.json", value)
        return self.resolve(inspection)

    def resolve(self, inspection):
        inspection.check()
        self.check_binding()
        self._check_revocations()
        path = self.directory(inspection) / "binding.json"
        if not path.exists():
            return None
        value = read_private(path)
        value.update(
            scope=Scope(**value["scope"]),
            capture_path=Path(value["capture_path"]),
            trace_path=Path(value["trace_path"]),
        )
        binding = ReviewBinding(**value)
        if (binding.capture_path, binding.trace_path) != (
            path.parent / "capture.json",
            path.parent / "public-trace.json",
        ):
            raise EvidenceRequired("Review capture source escaped its original storage")
        return InstalledReviews(
            self.key, self.revoked, (binding,), selected={inspection.digest: binding.reference}
        ).resolve(inspection)


class NativeReviewSource:
    def __init__(self, store, factory=None):
        self.store, self.factory = store, factory
        self._bound = (store, factory)

    def check_binding(self):
        if (self.store, self.factory) != self._bound:
            raise EvidenceRequired("Trusted review registration changed")
        self.store.check_binding()

    def resolve(self, inspection):
        self.check_binding()
        captured = self.store.resolve(inspection)
        if captured is not None or self.factory is None:
            return captured
        from .native import NativeReviewCapture

        producer = self.factory(inspection)
        if type(producer) is not NativeReviewCapture:
            raise EvidenceRequired("An actual independent native review producer is required")
        return producer.capture(inspection, self.store)
