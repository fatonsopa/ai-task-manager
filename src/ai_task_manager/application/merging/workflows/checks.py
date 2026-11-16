"""Derive change paths from the actual candidate; installed discovery owns inventory."""

from ai_task_manager.domains.checks.models import Availability, Change, Scope
from ai_task_manager.domains.work.errors import AccessDenied


class CandidateChecks:
    def __init__(self, checks, discover, select_retained=None):
        self.checks, self.discover = checks, discover
        self.select_retained = select_retained

    def restore(self, profile, grant, candidate, reference):
        if self.select_retained is None:
            raise AccessDenied("Retained candidate input selection is unavailable")
        profile.validate()
        self.select_retained(candidate)
        b = grant.binding
        scope = Scope(b.principal_id, b.project_id, b.session_id, b.task_id)
        snapshot = self.checks.reads.read(
            reference, scope, after_wait=lambda: self.select_retained(candidate)
        )
        self.checks._verify(snapshot["plan"])
        return reference

    def prepare(self, profile, grant, candidate, key):
        profile.validate()
        binding = grant.binding
        discovery = self.discover(candidate)
        if discovery.status != Availability.KNOWN:
            raise AccessDenied("Complete configured candidate impact discovery is required")
        paths = profile.query(
            profile.source["path"], "diff", "--name-only", "-z", grant.target_head, candidate.commit
        ).stdout
        changed = tuple(p.decode("utf-8", "strict") for p in paths.split(b"\0") if p)
        scope = Scope(binding.principal_id, binding.project_id, binding.session_id, binding.task_id)
        return self.checks.prepare(
            scope,
            binding.attempt_id,
            binding.source_digest,
            Change(Availability.KNOWN, changed, grant.target_head, candidate.commit),
            discovery,
            key,
        )
