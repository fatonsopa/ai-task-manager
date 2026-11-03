"""Persist only captures produced by the actual independent native reviewer."""

import hashlib
import hmac
import os
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.config.files import private_path, read_private, write_private
from ai_task_manager.domains.checks.models import canonical
from ai_task_manager.domains.work.errors import EvidenceRequired
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
        if not self.key.exists():
            with self.key.open("xb") as stream:
                self.key.chmod(0o600)
                stream.write(os.urandom(32))
        self.revoked = self.root / "revoked.json"
        if not self.revoked.exists():
            write_private(self.revoked, {"revoked": []})
        self.key_witness = FileWitness.capture(private_path(self.key))

    def _identity(self):
        info = self.root.stat()
        return (info.st_dev, info.st_ino, info.st_uid, info.st_mode)

    def check_binding(self):
        if self.root.resolve() != self.root or self._identity() != self._root_identity:
            raise EvidenceRequired("Original review capture directory changed")
        self.key_witness.check()

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

    def failed(self, directory, error):
        path = directory / "failure.json"
        if not path.exists():
            write_private(
                path,
                {
                    "type": type(error).__name__,
                    "message": str(error),
                    "outcome": "failed_or_unknown",
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
        write_private(trace_path, trace)
        record = {**record, "trace_sha256": sha(trace_path)}
        signature = hmac.new(
            self.key.read_bytes(), canonical(record).encode(), hashlib.sha256
        ).hexdigest()
        write_private(capture_path, {"record": record, "signature": signature})
        binding = ReviewBinding(
            record["reference"],
            inspection.digest,
            Scope(**inspection.context["scope"]),
            inspection.context["task_id"],
            record["operation_key"],
            capture_path,
            sha(capture_path),
            trace_path,
            sha(trace_path),
            record["reviewer_id"],
            record["host"],
            record["model"],
            record["provenance_ref"],
        )
        value = asdict(binding)
        value.update(capture_path=str(capture_path), trace_path=str(trace_path))
        write_private(directory / "binding.json", value)
        return self.resolve(inspection)

    def resolve(self, inspection):
        inspection.check()
        self.check_binding()
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
