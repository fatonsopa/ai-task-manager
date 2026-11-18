"""Reuse exact historical capability facts; current work authority remains separate."""

import math
from typing import Protocol

from ai_task_manager.config.files import digest

from .profile import prepare_profile
from .verification import require, verify_capture


class CaptureEnrollment(Protocol):
    def require(self, reference: str, profile_digest: str) -> dict: ...


class NormalConformance:
    def __init__(self, manifest, pins, context, journal, reference, *, enrollment):
        # These are original capture provenance, not cached current-task authority.
        self.manifest, self.context = manifest, context
        self.pins, self.journal, self.reference, self.enrollment = (
            pins,
            journal,
            reference,
            enrollment,
        )
        self._bound = (pins, journal, reference, enrollment)

    def require_admission(self, manifest, *, acquisition):
        return self.require_installation(manifest, acquisition=acquisition)

    def require_installation(self, manifest, *, acquisition=None):
        require(
            self.pins is self._bound[0]
            and self.journal is self._bound[1]
            and self.reference == self._bound[2]
            and self.enrollment is self._bound[3],
            "The original capability consumer changed",
        )
        manifest.check()
        self.pins.check()
        record, profile, _ = verify_capture(self.journal, self.reference)
        row = self.enrollment.require(
            self.reference,
            profile.fingerprint,
            **({"acquisition": acquisition} if acquisition is not None else {}),
        )
        require(
            type(row) is dict
            and set(row)
            == {"reference", "capture_hash", "profile_digest", "revoked", "valid_until"}
            and row["reference"] == self.reference
            and row["capture_hash"] == digest(record)
            and row["profile_digest"] == profile.fingerprint
            and row["revoked"] is False,
            "Actual completed capture is not durably enrolled or was revoked",
        )
        until = row["valid_until"]
        require(
            until is None
            or (
                type(until) in {int, float}
                and math.isfinite(until)
                and self.journal.clock() < until
            ),
            "Operator capability validity expired",
        )
        require(
            profile == prepare_profile(manifest, self.pins, profile.cli_version),
            "Current installed source/account/role recipe differs from captured capability",
        )
        return self.reference

    def require_native_conformance(self, manifest, policy, binding, journal, *, acquisition=None):
        self.require_installation(manifest, acquisition=acquisition)
        _, profile, _ = verify_capture(self.journal, self.reference)
        require(
            binding.host == profile.host
            and prepare_profile(manifest, self.pins, profile.cli_version, policy=policy) == profile,
            "Actual runtime policy differs from captured capability",
        )
        policy.check()
        # This is not a scope/permission/authentication grant. NormalAdmissions
        # separately checks this new task's permission and fresh actual startup.
        return self.reference

    def revoke(self):
        revoke = getattr(self.enrollment, "revoke", None)
        require(callable(revoke), "Trusted durable capability revocation is unavailable")
        return revoke(self.reference)
