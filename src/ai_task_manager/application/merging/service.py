"""One internal publication consumer; no shell route, status writer or check selector."""

from contextlib import contextmanager
from dataclasses import asdict

from ai_task_manager.domains.merging.contracts import Binding, Observation, Publication, sha, token
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    IdempotencyConflict,
    InvalidTransition,
    ValidationError,
)


class MergeService:
    def __init__(self, journal, authority, checks=None, *, publisher_factory=None):
        # Construction is a trusted composition port, never a model-facing tool.
        self.journal, self.authority, self.checks = journal, authority, checks
        self.publisher = publisher_factory(self) if publisher_factory else None

    def register(self, binding):
        if type(binding) is not Binding:
            raise ValidationError("An exact immutable binding is required")
        binding.validate()
        if self.authority is None:
            raise AccessDenied("Trusted registration authority is unavailable")
        self.authority.registration(binding)
        return self.journal.register(asdict(binding), binding.fingerprint)

    def _load(self, ref):
        operation = self.journal.current(ref)
        return operation, Binding(**operation["binding"])

    def _checked(self, operation, binding, request, evidence_ref):
        if self.checks is None:
            raise AccessDenied("Trusted current check evidence is unavailable")
        fact = self.checks.read(token(evidence_ref), binding)
        if type(fact) is not Observation or fact.evidence_ref != evidence_ref:
            raise AccessDenied("Invalid check observation")
        payload = fact.payload(operation, "checks")
        if set(payload) != {"head", "permitted", "contract_digest", "source_digest"}:
            raise AccessDenied("Incomplete check observation")
        sha(payload["head"], 40)
        sha(payload["contract_digest"])
        if (
            payload["head"] != request.new
            or payload["permitted"] is not True
            or payload["source_digest"] != binding.source_digest
        ):
            raise AccessDenied("Check evidence does not permit this exact candidate")
        return dict(payload, evidence_ref=evidence_ref)

    def prepare(self, ref, expected_version, key, request, checks_ref):
        if type(request) is not Publication:
            raise ValidationError("An exact publication value is required")
        request.validate()
        operation, binding = self._load(ref)
        if self.authority is None:
            raise AccessDenied("Trusted publication authority is unavailable")
        self.authority.registration(binding)
        payload = asdict(request)
        replay = self.journal.replay(ref, key, ["publication_intent", payload, checks_ref])
        if replay is not None:
            return replay  # Historical intent only; every new effect rechecks authority/evidence.
        self.authority.before_start(binding)
        self.authority.check_current(binding)
        check = self._checked(operation, binding, request, checks_ref)
        if "publication_intent" in operation["facts"]:
            raise InvalidTransition("An existing publication intent cannot be replaced")
        facts = dict(operation["facts"], publication_intent=payload, checks=check)
        return self.journal.commit(
            ref,
            expected_version,
            key,
            "publication_intent",
            facts,
            payload,
            request=["publication_intent", payload, checks_ref],
        )

    @contextmanager
    def authority_boundary(self, ref):
        operation, binding = self._load(ref)
        facts = operation["facts"]
        if "publication_intent" not in facts or "checks" not in facts:
            raise AccessDenied("Current publication intent and checks are required")
        local = facts.get("local_git", {})
        if local.get("stage") == "uncertain" or local.get("result"):
            raise AccessDenied("Observed publication requires reconciliation")
        request = Publication(**facts["publication_intent"])
        self._checked(operation, binding, request, facts["checks"]["evidence_ref"])
        boundary = getattr(self.authority, "authority_boundary", None)
        if boundary is None:
            raise AccessDenied("Trusted publication authority boundary is unavailable")
        self.authority.check_current(binding)
        # The port serializes only authority and the bounded physical effect.
        # Provider reads and Git waits must remain outside its transaction.
        with boundary(binding):
            yield

    def publish(self, ref, expected_version, key, request, checks_ref):
        if self.publisher is None:
            raise AccessDenied("Configured Git publication transport is unavailable")
        if type(request) is not Publication:
            raise ValidationError("An exact publication value is required")
        request.validate()
        operation, binding = self._load(ref)
        if self.authority is None:
            raise AccessDenied("Trusted publication authority is unavailable")
        self.authority.registration(
            binding
        )  # Historical replay still belongs to this caller scope.
        if operation["facts"].get("local_git"):
            # Existing durable input is reconciled, never launched again. Committed
            # history remains truthful after checks expire or current config drifts.
            return self.publisher.publish(ref, expected_version, key, request)
        if "publication_intent" in operation["facts"]:
            if operation["facts"]["publication_intent"] != asdict(request):
                raise IdempotencyConflict("Publication differs from its prepared input")
            self.authority.check_current(binding)
            self._checked(operation, binding, request, operation["facts"]["checks"]["evidence_ref"])
            return self.publisher.publish(ref, expected_version, key, request)
        prepared = self.prepare(ref, expected_version, "prepare:" + key, request, checks_ref)
        return self.publisher.publish(ref, prepared["version"], key, request)
