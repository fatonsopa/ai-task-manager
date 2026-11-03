"""A short-lived merge witness from actual publication, never semantic review."""

import time
from pathlib import Path

from ai_task_manager.adapters.workflow_reviews.models import FileWitness
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Evidence

PREFIX = "merge-publication:"


class MergeEvidence:
    def __init__(self, clock=time.time):
        self.clock, self.prepared = clock, {}

    def prepare(self, core, handle, plan, candidate, publication, consumer, checks_ref):
        task = core.get_task(handle, plan.grant.binding.task_id)
        _, scope = core.session_binding(handle)
        request = consumer.grant.publication
        row = consumer.service.journal.current(consumer.registration["ref"])
        saved = row["facts"].get("local_git", {}).get("result")
        if (
            publication != saved
            or publication.get("publication") != "committed"
            or publication.get("quiescent") is not True
            or publication.get("repair_required")
            or (publication["old"], publication["new"])
            != (plan.grant.target_head, candidate.commit)
            or row["fingerprint"] != consumer.grant.binding.fingerprint
            or task.prose_digest != plan.grant.binding.prose_digest
            or task.human_hold
            or task.delivered_once
        ):
            raise EvidenceRequired("Exact current successful owned publication is required")
        consumer.checks.read(checks_ref, consumer.grant.binding)
        profile = consumer.authority.profile
        record = profile.validate(
            consumer.grant.binding.worktree_id, consumer.grant.binding.repository_id
        )
        common = Path(record["common"])
        witnesses = tuple(
            FileWitness.capture(p)
            for p in [
                common / record["target_ref"],
                common / request.source_ref,
                common / plan.grant.source_ref,
                common / "packed-refs",
                common / "config",
            ]
        )
        current = profile.reference(record["path"], record["target_ref"])
        if (
            profile.reference(record["path"], plan.grant.source_ref) != plan.grant.source_head
            or profile.reference(record["path"], request.source_ref) != candidate.commit
            or profile.query(
                record["path"], "merge-base", "--is-ancestor", candidate.commit, current, ok=(0, 1)
            ).returncode
        ):
            raise EvidenceRequired("Published candidate ancestry is not current")
        for witness in witnesses:
            witness.check()
        ref = PREFIX + digest(
            [
                consumer.registration["ref"],
                consumer.grant.binding.fingerprint,
                candidate.commit,
                checks_ref,
            ]
        )
        fact = Evidence(
            "merge",
            scope,
            task.id,
            {
                "prose_digest": task.prose_digest,
                "commit": candidate.commit,
                "attempt_id": plan.grant.binding.attempt_id,
                "source_head": plan.grant.source_head,
                "target_ref": record["target_ref"],
                "publication_ref": consumer.registration["ref"],
                "checks_ref": checks_ref,
                "candidate_ref": candidate.candidate_ref,
                "source_digest": plan.grant.binding.source_digest,
                "publication_binding": consumer.grant.binding.fingerprint,
            },
            self.clock() + 5,
        )
        self.prepared[ref] = (fact, witnesses)
        return ref

    def verify_evidence(self, reference):
        try:
            fact, witnesses = self.prepared[reference]
        except KeyError:
            raise EvidenceRequired("Prepare actual publication evidence outside Core") from None
        if fact.expires_at <= self.clock():
            raise EvidenceRequired("Prepared publication evidence expired")
        for witness in witnesses:
            witness.check()
        return Evidence(fact.kind, fact.scope, fact.subject, dict(fact.payload), fact.expires_at)
