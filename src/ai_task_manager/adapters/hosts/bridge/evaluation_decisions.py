"""Scoped evaluation decisions retain the existing sole decision issuer and Core policy."""

from ai_task_manager.adapters.decisions.authority import DecisionAuthority
from ai_task_manager.domains.work.errors import AccessDenied

from .evaluation_enrollment import EvaluationGrant


class EvaluationDecisions:
    def __init__(self, grant, authority_journal):
        if not isinstance(grant, EvaluationGrant):
            raise AccessDenied("A bound lead-installed evaluation grant is required")
        self.grant = self._original_grant = grant
        self.authority = self._original_authority = DecisionAuthority(authority_journal, grant)
        self._prepared = {}

    def _current(self):
        if self.grant is not self._original_grant or self.authority is not self._original_authority:
            raise AccessDenied("The evaluation decision composition changed")

    def decision(self, scope, task, target, action, *, grant_ref=None, human_ref=None, reason=""):
        self._current()
        self.grant.require_task(task)
        if (
            scope != self.grant.scope
            or grant_ref != self.grant.ref
            or human_ref is not None
            or target == "In Review"
            or task.observed_status == "In Review"
        ):
            raise AccessDenied("Evaluation cannot authorize human acceptance or another scope")
        prepared = self.grant.prepare_current()
        ref = self.authority.decision(
            scope, task, target, action, grant_ref=grant_ref, reason=reason
        )
        self._prepared[ref] = prepared
        return ref

    def verify_evidence(self, ref):
        """Core consumption checks only prepared local metadata/sticky/kernel state.

        The nonblocking guard observation ends before Core commits. This is a
        current point check, not an atomic lock across Core's transaction.
        """
        self._current()
        prepared = self._prepared.get(ref)
        if prepared is None:
            raise AccessDenied("This evaluation did not prepare that decision")
        prepared.check()
        evidence = self.authority.journal.verify_evidence(ref)
        if (
            evidence.kind != "decision"
            or evidence.scope != self.grant.scope
            or evidence.subject != self.grant._task_id
            or evidence.payload.get("actor") != "agent"
        ):
            raise AccessDenied("The issuer does not own this evaluation decision")
        return evidence

    def human_plan(self, *args, **kwargs):
        raise AccessDenied("Evaluation grants cannot issue human acceptance")
