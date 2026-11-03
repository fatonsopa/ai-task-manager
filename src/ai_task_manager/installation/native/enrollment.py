"""Durable historical capability intake requires the actual capture producer."""

import math
import stat
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.conformance import (
    CompletedCapture,
    NormalConformance,
    ProbeContext,
    ProfileSources,
)
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.config.files import digest, private_path
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope


def physical(path):
    path = private_path(path)
    info = path.stat()
    return {
        "path": str(path),
        "device": info.st_dev,
        "inode": info.st_ino,
        "owner": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
    }


class CaptureEnrollments:
    def __init__(self, store):
        self.store = store

    def enroll(self, completed, journal, sources, *, valid_until=None):
        if (
            type(completed) is not CompletedCapture
            or type(journal) is not HostJournal
            or (type(sources) is not ProfileSources)
        ):
            raise AccessDenied("Actual completed capture, journal and source objects required")
        if valid_until is not None and (
            type(valid_until) not in {int, float}
            or not math.isfinite(valid_until)
            or valid_until <= journal.clock()
        ):
            raise AccessDenied("Operator capability validity must be finite and current")
        exported = completed.export()
        if exported.get("version") != 2:
            raise AccessDenied("Task-bound historical format cannot become a stable capability")
        record = journal._read(exported["reference"], "trace", fresh=False)
        sources.check()
        profile = exported["profile"]
        if (
            digest(record) != exported["capture_hash"]
            or profile["source_hashes"] != [[n, h] for n, _, h in sources.files]
            and profile["source_hashes"] != tuple((n, h) for n, _, h in sources.files)
        ):
            raise AccessDenied("Actual capture or logical source provenance differs")
        row = {
            "reference": exported["reference"],
            "capture_hash": exported["capture_hash"],
            "profile_digest": exported["profile_digest"],
            "revoked": False,
            "valid_until": valid_until,
            "capture": exported,
            "journal": physical(journal.path),
            "key": physical(journal.key_path),
            "sources": {
                "files": {n: [str(p), h] for n, p, h in sources.files},
                "versions": dict(sources.versions),
                "instructions": list(sources.instruction_ids),
            },
        }
        with self.store.transaction() as conn:
            old = self.store.read(conn, "native_conformance", row["reference"])
            if old is not None and digest(old) != digest(row):
                raise AccessDenied("Existing capability enrollment cannot be replaced or extended")
            if old is None:
                self.store.put(conn, "native_conformance", row["reference"], row)
                self.store.event(
                    conn,
                    "conformance_enrolled",
                    None,
                    {"reference": row["reference"], "capture_hash": row["capture_hash"]},
                )
        return self.require(row["reference"], row["profile_digest"])

    def _read(self, reference):
        with self.store.transaction() as conn:
            row = self.store.read(conn, "native_conformance", reference)
        if row is None:
            raise Unavailable("Normal conformance capture is not enrolled")
        required = {
            "reference",
            "capture_hash",
            "profile_digest",
            "revoked",
            "valid_until",
            "capture",
            "journal",
            "key",
            "sources",
        }
        if (
            set(row) != required
            or row["reference"] != reference
            or type(row["revoked"]) is not bool
        ):
            raise AccessDenied("Durable capability enrollment is incomplete or changed")
        for name in ["journal", "key"]:
            if physical(row[name]["path"]) != row[name]:
                raise AccessDenied("Enrolled capability journal/key physical identity changed")
        return row

    def require(self, reference, profile_digest):
        row = self._read(reference)
        if row["profile_digest"] != profile_digest:
            raise AccessDenied("Capability enrollment belongs to another stable profile")
        return {
            k: row[k]
            for k in ["reference", "capture_hash", "profile_digest", "revoked", "valid_until"]
        }

    def revoke(self, reference):
        row = self._read(reference)
        with self.store.transaction() as conn:
            current = self.store.read(conn, "native_conformance", reference)
            if current != row:
                raise AccessDenied("Capability changed during revocation")
            current["revoked"] = True
            self.store.put(conn, "native_conformance", reference, current)
            self.store.event(conn, "conformance_revoked", None, {"reference": reference})

    def consumer(self, manifest, reference):
        row = self._read(reference)
        sources = ProfileSources(
            row["sources"]["files"],
            row["sources"]["versions"],
            tuple(row["sources"]["instructions"]),
        )
        journal = HostJournal(row["journal"]["path"], row["key"]["path"])
        if digest(journal._read(reference, "trace", fresh=False)) != row["capture_hash"]:
            raise AccessDenied("Enrolled immutable capture changed")
        data = dict(row["capture"]["provenance"]["context"])
        data["scope"] = Scope(**data["scope"])
        data["fixture_root"] = Path(data["fixture_root"])
        data["fixture_identity"] = tuple(data["fixture_identity"])
        data["case_ids"] = tuple(data["case_ids"])
        context = ProbeContext(**data)
        result = NormalConformance(manifest, sources, context, journal, reference, enrollment=self)
        result.require_installation(manifest)
        return result
