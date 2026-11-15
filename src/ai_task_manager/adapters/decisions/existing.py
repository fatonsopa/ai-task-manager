"""An observed lead selection grants binding, never human acceptance or work."""

from dataclasses import asdict, dataclass

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import Scope

PREFIX = "session-issue-selection:"


@dataclass(frozen=True)
class IssueSelection:
    scope: Scope
    issue_id: str
    discovery_digest: str
    claim_store: dict
    source_hash: str
    expires_at: float
    source_ref: str | None = None


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
            observed.source_ref or observed.source_hash,
            observed.expires_at,
        )

    def validate(self, reference, scope, issue_id, discovery_digest, claim_store, observation_ref):
        observed = self.observer.verify_issue_selection(observation_ref)
        proof = self.journal.verify_evidence(reference)
        if (
            not isinstance(observed, IssueSelection)
            or (observed.scope, observed.issue_id, observed.discovery_digest, observed.claim_store)
            != (scope, issue_id, discovery_digest, claim_store)
            or observed.expires_at <= self.journal.clock()
            or (proof.kind, proof.scope, proof.subject, proof.payload)
            != (
                "existing_issue_selection",
                scope,
                issue_id,
                {"discovery_digest": discovery_digest, "claim_store": claim_store},
            )
        ):
            raise AccessDenied("Saved selection does not match its current observed source")
        return proof

    def current(self, reference):
        proof = self.journal.verify_evidence(reference)
        source = self.journal._get(reference, "decision-proof:")["source_ref"]
        if isinstance(source, str) and source.startswith(PREFIX):
            observed = self.observer.verify_issue_selection(source)
            if (proof.kind, proof.scope, proof.subject, proof.payload) != (
                "existing_issue_selection",
                observed.scope,
                observed.issue_id,
                {
                    "discovery_digest": observed.discovery_digest,
                    "claim_store": observed.claim_store,
                },
            ):
                raise AccessDenied("Selection differs from its current installed observation")
        return proof


def capture(observer, scope, discovery, claim_store, invocation_id, arguments):
    """Record the actual delegated select operation, never a claimed human decision."""
    grant = observer.verify_enrollment(observer.pointer["reference"])
    if (
        grant.scope != scope
        or "existing_issue_selection" not in grant.actions
        or discovery.kind != "existing_issue_snapshot"
        or discovery.scope != scope
        or discovery.payload.get("claim_store") != claim_store
        or discovery.expires_at is None
        or discovery.expires_at <= observer.journal.clock()
    ):
        raise EvidenceRequired("Current explicit existing-issue selection delegation required")
    fingerprint = digest({"operation": "task_select", "arguments": arguments})
    expected_id = digest(
        {"scope": asdict(scope), "key": arguments["operation_key"], "claim_store": claim_store}
    )
    if invocation_id != expected_id:
        raise AccessDenied("Selection belongs to another invocation or enrolled store")
    with observer.journal.transaction() as conn:
        row = conn.execute(
            "SELECT fingerprint FROM invocations WHERE id=?", (invocation_id,)
        ).fetchone()
    if row is None or row[0] != fingerprint:
        raise AccessDenied("An actual exact installed selection invocation is required")
    values = {
        "enrollment": grant.source_ref,
        "scope": asdict(scope),
        "issue_id": discovery.subject,
        "discovery_digest": discovery.payload["discovery_digest"],
        "claim_store": claim_store,
        "invocation_id": invocation_id,
        "fingerprint": fingerprint,
        "expires_at": min(discovery.expires_at, grant.expires_at),
    }
    return observer.journal._put(PREFIX, {**values, "source_hash": digest(values)})


def verify_receipt(journal, reference):
    """Recheck local source authority during Core consumption; no provider I/O."""
    from .observations import current_enrollment

    data = journal._get(reference, PREFIX)
    enrollment = current_enrollment(journal, data["enrollment"], data["scope"])
    with journal.transaction() as conn:
        row = conn.execute(
            "SELECT fingerprint FROM invocations WHERE id=?", (data["invocation_id"],)
        ).fetchone()
    if (
        "existing_issue_selection" not in enrollment["actions"]
        or row is None
        or row[0] != data["fingerprint"]
        or data["expires_at"] > enrollment["expires_at"]
        or data["source_hash"]
        != digest({k: v for k, v in data.items() if k not in {"ref", "source_hash"}})
    ):
        raise AccessDenied("Selection source or current delegation changed")
    return IssueSelection(
        Scope(**data["scope"]),
        data["issue_id"],
        data["discovery_digest"],
        data["claim_store"],
        data["source_hash"],
        data["expires_at"],
        reference,
    )
