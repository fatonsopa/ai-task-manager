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
        evidence_refs["existing_source"] = service._required("existing").queue_readback(
            task.id, version, revision, data["operation_key"]
        )
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


def settle_external(service, data, invocation):
    """Settle an existing human observation; never issue human or stop evidence."""
    import json

    from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired

    from .models import result
    from .projection import task_view

    values = {
        key: data[key]
        for key in (
            "task_id",
            "observation_id",
            "expected_version",
            "decision_revision",
            "cause",
            "operation_key",
        )
    }
    values["expected_decision_revision"] = values.pop("decision_revision")
    values["evidence_refs"] = data.get("evidence_refs", {})
    recovered = service.core.external.recover_settle(service.handle, **values)
    if recovered is not None:
        return result(
            task_view(service, recovered),
            "confirmed" if recovered.sync_state == "confirmed" else "pending",
        )
    task = service.core.get_task(service.handle, data["task_id"])
    require_owned(service, task)
    service.core.context.check(task, data["expected_version"], data["decision_revision"])
    original = next(
        (
            row
            for row in service.core.external.observations(service.handle, task.id)
            if row["id"] == data["observation_id"] and row["state"] == "pending"
        ),
        None,
    )
    if original is None:
        raise EvidenceRequired("Current pending external observation is required")
    observations = service._required("decisions").observations
    inbound = service._required("inbound")

    def check():
        grant = observations.verify_enrollment(service.grant_ref)
        if (
            grant.scope != service.scope
            or grant.logical_session != service.scope.logical_session
            or grant.expires_at <= service.decisions.journal.clock()
        ):
            raise AccessDenied("External settlement belongs to another enrollment")
        inbound.client.check_connection(service.scope, refresh=True)
        if service.seal:
            service.seal.check()
        if service.claim_store:
            service.claim_store.check()

    check()
    snapshot = inbound.client.read_issue(task.issue_id)
    check()
    inbound.client.require_delegate(snapshot)
    recorded = json.loads(original["payload"])
    fresh = {
        "issue_id": snapshot.id,
        "source_revision": snapshot.revision,
        "status": snapshot.status,
        "title": snapshot.title,
        "body": snapshot.body,
        "assigned_to_owner": snapshot.assignee_id == service.scope.principal,
    }
    if any(recorded.get(key) != value for key, value in fresh.items()):
        raise EvidenceRequired("Provider changed after the original human observation")
    service.core.context.check(
        service.core.get_task(service.handle, task.id),
        data["expected_version"],
        data["decision_revision"],
    )
    check()
    changed = service.core.external.settle(service.handle, **values)
    return result(task_view(service, changed))
