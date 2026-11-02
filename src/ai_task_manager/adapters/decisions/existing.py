"""An observed lead selection grants binding, never human acceptance or work."""

from dataclasses import dataclass

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope


@dataclass(frozen=True)
class IssueSelection:
    scope: Scope
    issue_id: str
    discovery_digest: str
    claim_store: dict
    source_hash: str
    expires_at: float


class RejectSelections:
    def verify_issue_selection(self, observation_ref):
        raise AccessDenied("A trusted lead-selection observer is not configured")


class SelectionAuthority:
    def __init__(self, journal, observer=None):
        self.journal, self.observer = journal, observer or RejectSelections()

    def issue(self, scope, issue_id, discovery_digest, claim_store, observation_ref):
        observed = self.observer.verify_issue_selection(observation_ref)
        if (
            not isinstance(observed, IssueSelection)
            or observed.scope != scope
            or observed.issue_id != issue_id
            or observed.discovery_digest != discovery_digest
            or observed.claim_store != claim_store
            or not isinstance(observed.source_hash, str)
            or len(observed.source_hash) != 64
            or any(character not in "0123456789abcdef" for character in observed.source_hash)
            or observed.expires_at <= self.journal.clock()
        ):
            raise AccessDenied("Observed selection does not match this issue and installation")
        return self.journal._evidence(
            "existing_issue_selection",
            scope,
            issue_id,
            {"discovery_digest": discovery_digest, "claim_store": claim_store},
            observed.source_hash,
            observed.expires_at,
        )
