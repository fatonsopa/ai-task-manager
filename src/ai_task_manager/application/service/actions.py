"""Translate named actions; the core alone validates status-pair policy."""

from ai_task_manager.domains.work.models import Cause, IssueStatus

from .authorization import authorize
from .projection import require_owned

ACTION = {
    "enqueue": (Cause.ENQUEUE, IssueStatus.TODO),
    "dequeue": (Cause.DEQUEUE, IssueStatus.BACKLOG),
    "hold": (Cause.HOLD, IssueStatus.ON_HOLD),
    "request_review": (Cause.REQUEST_REVIEW, IssueStatus.IN_REVIEW),
    "review_decision": (Cause.REVIEW_DECISION, None),
    "cancel": (Cause.CANCEL, IssueStatus.CANCELED),
    "duplicate": (Cause.DUPLICATE, IssueStatus.DUPLICATE),
    "merge": (Cause.MERGE, IssueStatus.IN_DEV),
    "stage": (Cause.STAGE, IssueStatus.IN_STAGING),
    "release": (Cause.RELEASE, IssueStatus.DONE),
    "non_code_complete": (Cause.NON_CODE_COMPLETE, IssueStatus.DONE),
}


def perform(service, data, invocation):
    task = service.core.get_task(service.handle, data["task_id"])
    require_owned(service, task)
    cause, target = ACTION[data["action"]]
    if target is None:
        target = IssueStatus(data["review_target"])
    reason = ""
    if data.get("reason_writing_ref"):
        reason = (
            service.writer.approvals_for_progress(service.scope, task.id, task.prose_digest)
            .prose(data["reason_writing_ref"])
            .body
        )
    decision, version, revision = authorize(
        service, task, target, data["action"], {**data, "reviewed_reason": reason}, invocation
    )
    evidence_refs = dict(data.get("evidence_refs", {}))
    if target == IssueStatus.TODO and service.core.existing.is_imported(service.handle, task.id):
        current = service.core.get_task(service.handle, task.id)
        evidence_refs["existing_source"] = service._required("existing").guard(current)
    updated = service.core.transitions.transition(
        service.handle,
        task.id,
        target,
        cause,
        version,
        revision,
        decision,
        evidence_refs,
        data["operation_key"],
    )
    return service.confirm(updated)
