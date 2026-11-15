"""Interpret only pre-enrolled grants and independently captured observations."""

from dataclasses import dataclass

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.transitions import human_hold_required


@dataclass(frozen=True)
class HostGrant:
    scope: object
    logical_session: str
    actions: tuple[str, ...]
    source_ref: str
    expires_at: float


@dataclass(frozen=True)
class HumanInstruction:
    scope: object
    task_id: str
    target: str
    revision: int
    reason: str
    source_hash: str
    task_version: int | None = None
    prose_digest: str | None = None
    proposed_prose_digest: str | None = None
    decision_request_id: str | None = None
    decision_option_id: str | None = None
    decision_digest: str | None = None
    decision_actor_id: str | None = None


@dataclass(frozen=True)
class HostObservation:
    scope: object
    event_id: str
    session_id: str
    stage: str
    trace_hash: str
    source_ref: str | None = None
    expires_at: float | None = None


class RejectObservations:
    def verify_enrollment(self, ref):
        raise EvidenceRequired("Trusted host enrollment is unavailable")

    def verify_instruction(self, ref):
        raise EvidenceRequired("Actual human instruction observation is unavailable")

    def verify_host_observation(self, ref):
        raise EvidenceRequired("Actual host transcript observation is unavailable")


class DecisionAuthority:
    def __init__(self, journal, observations=None):
        self.journal = journal
        self.observations = observations or RejectObservations()

    def decision(self, scope, task, target, action, *, grant_ref=None, human_ref=None, reason=""):
        human = task.observed_status == "In Review" or target == "In Review"
        if human_ref:
            source = self.observations.verify_instruction(human_ref)
            if not isinstance(source, HumanInstruction) or (
                source.scope,
                source.task_id,
                source.target,
                source.revision,
            ) != (scope, task.id, target, task.decision_revision):
                raise EvidenceRequired(
                    "Human instruction does not authorize this task and revision"
                )
            if source.decision_request_id is not None:
                raise EvidenceRequired("A decision option is not task-transition acceptance")
            if source.prose_digest != task.prose_digest:
                raise EvidenceRequired("Human acceptance belongs to another task version or scope")
            actor, source_ref, actual_reason = "human", source.source_hash, source.reason
            expires_at = self.journal.clock() + 300
        else:
            if human:
                raise EvidenceRequired("Explicit observed human acceptance is required")
            grant = self.observations.verify_enrollment(grant_ref)
            if (
                not isinstance(grant, HostGrant)
                or grant.scope != scope
                or (action not in grant.actions or grant.expires_at <= self.journal.clock())
            ):
                raise AccessDenied("Delegated action is outside the enrolled host grant")
            actor, source_ref, actual_reason = "agent", grant.source_ref, reason
            expires_at = grant.expires_at
        return self.journal._evidence(
            "decision",
            scope,
            task.id,
            {
                "target": target,
                "revision": task.decision_revision,
                "actor": actor,
                "reason": actual_reason,
            },
            source_ref,
            expires_at,
        )

    def _followup_grant(self, scope, grant_ref):
        from .observations import PREFIX

        grant = self.observations.verify_enrollment(grant_ref)
        if (
            not isinstance(grant, HostGrant)
            or grant.scope != scope
            or grant.logical_session != scope.logical_session
            or "delivered_followup" not in grant.actions
            or grant.expires_at <= self.journal.clock()
            or grant.source_ref != grant_ref
            or not isinstance(grant_ref, str)
            or not grant_ref.startswith(PREFIX)
        ):
            raise AccessDenied("Delivered follow-up is outside the enrolled host grant")
        return grant

    def delivered_followup(self, scope, parent, prose_digest, *, grant_ref):
        """Issue exact new-work authority after scoped intake verifies parent and prose."""
        grant = self._followup_grant(scope, grant_ref)
        return self.journal._evidence(
            "delivered_followup",
            scope,
            parent.id,
            {"prose_digest": prose_digest, "target": "Backlog"},
            grant.source_ref,
            min(grant.expires_at, self.journal.clock() + 300),
        )

    def validate_followup_source(self, reference, scope, parent_id):
        """Refresh enrolled source continuity outside Core before an uncommitted effect."""
        proof = self.journal.verify_evidence(reference)
        if (proof.kind, proof.scope, proof.subject) != ("delivered_followup", scope, parent_id):
            raise EvidenceRequired("Evidence does not match delivered follow-up, scope and parent")
        record = self.journal._get(reference, "decision-proof:")
        self._followup_grant(scope, record["source_ref"])

    def human_plan(self, scope, task, target, action, human_ref, proposed_prose_digest=None):
        if human_ref.startswith("decision-proof:"):
            if human_ref != task.decision_ref:
                raise EvidenceRequired("Recorded human intent has been superseded")
            observed = self.journal.verify_evidence(human_ref)
            expected = (target, task.prose_digest, task.decision_revision)
            actual = (
                observed.payload.get("target"),
                observed.payload.get("prose_digest"),
                observed.payload.get("previous_revision", -1) + 1,
            )
            if (observed.kind, observed.scope, observed.subject) != (
                "human_decision",
                scope,
                task.id,
            ) or actual != expected:
                raise EvidenceRequired("Recorded human intent does not authorize this destination")
            if (
                action == "revise"
                and observed.payload.get("proposed_prose_digest") != proposed_prose_digest
            ):
                raise EvidenceRequired("Recorded scope revision does not match approved prose")
            decision = self.journal._evidence(
                "decision",
                scope,
                task.id,
                {
                    "target": target,
                    "revision": task.decision_revision,
                    "actor": "human",
                    "reason": observed.payload["reason"],
                },
                human_ref,
                self.journal.clock() + 300,
            )
            return {"decision_ref": decision, "already_recorded": True}
        source = self.observations.verify_instruction(human_ref)
        if not isinstance(source, HumanInstruction) or (
            source.scope,
            source.task_id,
            source.target,
            source.revision,
            source.prose_digest,
        ) != (scope, task.id, target, task.decision_revision, task.prose_digest):
            raise EvidenceRequired(
                "Observed human instruction does not match this work and revision"
            )
        if source.decision_request_id is not None:
            raise EvidenceRequired("A decision option is not task-transition acceptance")
        if human_ref.startswith("session-human-receipt:") and source.task_version != task.version:
            raise EvidenceRequired("Human instruction belongs to another task version")
        if action == "revise" and source.proposed_prose_digest != proposed_prose_digest:
            raise EvidenceRequired("Observed scope revision does not match the new approved prose")
        expires = self.journal.clock() + 300
        observed = self.journal._evidence(
            "human_decision",
            scope,
            task.id,
            {
                "previous_revision": task.decision_revision,
                "hold": human_hold_required(target),
                "actor": "human",
                "reason": source.reason,
                "target": target,
                "prose_digest": task.prose_digest,
                "proposed_prose_digest": proposed_prose_digest,
            },
            source.source_hash,
        )
        decision = self.journal._evidence(
            "decision",
            scope,
            task.id,
            {
                "target": target,
                "revision": task.decision_revision + 1,
                "actor": "human",
                "reason": source.reason,
            },
            source.source_hash,
            expires,
        )
        return {"human_decision_ref": observed, "decision_ref": decision}

    def resume(self, scope, session_id, observation_ref):
        grant = self.observations.verify_enrollment(observation_ref)
        if (
            not isinstance(grant, HostGrant)
            or grant.scope != scope
            or "resume" not in grant.actions
            or grant.expires_at <= self.journal.clock()
        ):
            raise AccessDenied("Host enrollment does not authorize session resume")
        return self.journal._evidence(
            "session_resume", scope, session_id, {}, grant.source_ref, grant.expires_at
        )

    def notification(self, scope, event_id, session_id, stage, observation_ref):
        observed = self.observations.verify_host_observation(observation_ref)
        if (
            not isinstance(observed, HostObservation)
            or (observed.scope, observed.event_id, observed.session_id, observed.stage)
            != (scope, event_id, session_id, stage)
            or not observed.trace_hash
        ):
            raise EvidenceRequired("Host observation does not match the event and receipt stage")
        return self.journal._evidence(
            "notification_" + stage,
            scope,
            event_id,
            {
                "target_session_id": session_id,
                "trace_hash": observed.trace_hash,
            },
            observed.source_ref or observed.trace_hash,
            observed.expires_at,
        )

    def validate_notification_source(self, scope, event_id, session_id, stage, reference, proof):
        """Refresh external source continuity before an uncommitted receipt effect."""
        observed = self.observations.verify_host_observation(reference)
        saved = self.journal.verify_evidence(proof)
        if (
            (observed.scope, observed.event_id, observed.session_id, observed.stage)
            != (scope, event_id, session_id, stage)
            or (saved.kind, saved.scope, saved.subject)
            != ("notification_" + stage, scope, event_id)
            or saved.payload != {"target_session_id": session_id, "trace_hash": observed.trace_hash}
        ):
            raise EvidenceRequired("Notification recovery source differs from the original proof")
