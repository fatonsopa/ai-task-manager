"""Prepare exact captured-review facts; expose no public evidence-mint operation."""

import hashlib
import hmac
import json
import time
from dataclasses import asdict

from ai_task_manager.config.files import private_path, strict_json
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Evidence

from .models import CapturedReview, FileWitness
from .source import MissingReviews

PREFIX = "code-worker-review:"
KINDS = frozenset({"work_unit_complete"})


class CodeWorkerUnitIssuer:
    prefix = PREFIX
    kinds = KINDS

    def __init__(self, artifacts, issuer_key_path, reviews=None, *, clock=time.time):
        self.artifacts, self.reviews, self.clock = artifacts, reviews or MissingReviews(), clock
        self.key_path = private_path(issuer_key_path)
        self._key = self.key_path.read_bytes()
        if len(self._key) != 32:
            raise EvidenceRequired("An installed private issuer key is required")
        self._key_witness = FileWitness.capture(self.key_path)
        self._prepared = {}
        self.last_wait_reason = None
        self._bound = (self.artifacts, self.reviews, self.clock)

    def _current(self):
        if any(a is not b for a, b in zip((self.artifacts, self.reviews, self.clock), self._bound)):
            raise EvidenceRequired(
                "The configured review issuer changed; prepare with its new owner"
            )
        self.artifacts.check_binding()
        check = getattr(self.reviews, "check_binding", None)
        if callable(check):
            check()

    def lookup(self, scope, task, artifact_digest):
        self._current()
        self.last_wait_reason = None
        if not task.code_bearing:
            raise EvidenceRequired("Code worker unit issuer requires code work")
        inspection = self.artifacts.prepare(scope, task, artifact_digest)
        if inspection is None:
            self.last_wait_reason = "verified_artifact_missing"
            return None
        self._key_witness.check()
        captured = self.reviews.resolve(inspection)
        if captured is None:
            self.last_wait_reason = "captured_review_missing"
            return None
        if type(captured) is not CapturedReview or captured.context_digest != inspection.digest:
            raise EvidenceRequired("Installed reviewer returned an unrelated capture")
        if captured.reviewer_id == inspection.context["author"]["worker_id"]:
            raise EvidenceRequired("Code worker cannot review its own units")
        criteria = tuple(c[0] for c in inspection.context["criteria"])
        now = self.clock()
        if not captured.accepted(criteria, now):
            self.last_wait_reason = "review_rejected"
            return None
        witnesses = inspection.witnesses + captured.witnesses + (self._key_witness,)
        for witness in witnesses:
            witness.check()
        expires_at = min(now + 5, captured.expires_at)
        output = {"units": {}}
        for kind, unit in [("work_unit_complete", unit) for unit in task.required_work]:
            payload = {
                "prose_digest": task.prose_digest,
                "artifact": artifact_digest,
                "artifact_digest": artifact_digest,
                "artifacts": inspection.context["inspection"]["artifacts"],
                "source_head": inspection.context["source_version"]["head"],
                "review_ref": captured.reference,
                "reviewer_id": captured.reviewer_id,
                "host": captured.host,
                "model": captured.model,
                "provenance_ref": captured.provenance_ref,
                "trace_sha256": captured.trace_sha256,
                "capture_sha256": captured.source_digest,
                "criteria": list(criteria),
            }
            payload["unit"] = unit
            fact = {"kind": kind, "scope": asdict(scope), "subject": task.id, "payload": payload}
            # Stable captured-source identity; the expiring physical witness is
            # renewed only by another explicit outside-Core lookup.
            raw = json.dumps(fact, sort_keys=True, separators=(",", ":"))
            ref = PREFIX + hmac.new(self._key, raw.encode(), hashlib.sha256).hexdigest()
            self._prepared[ref] = (raw, witnesses, expires_at)
            output["units"][unit] = ref
        return output

    def verify_evidence(self, reference):
        self._current()
        value = self._prepared.get(reference) if isinstance(reference, str) else None
        if value is None:
            raise EvidenceRequired("Unknown or unprepared workflow review reference")
        raw, witnesses, expires_at = value
        if self.clock() >= expires_at:
            raise EvidenceRequired("Prepared review expired; prepare again outside Core")
        for witness in witnesses:
            witness.check()
        fact = strict_json(raw)
        from ai_task_manager.domains.work.models import Scope

        return Evidence(
            fact["kind"], Scope(**fact["scope"]), fact["subject"], fact["payload"], expires_at
        )
