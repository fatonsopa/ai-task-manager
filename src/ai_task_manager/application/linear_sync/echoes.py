"""Classify pending outbound snapshots using the scoped send journal."""

import json
from dataclasses import asdict


def retained_responses(journal, scope, operation):
    """Retained responses from this scoped operation's current attempt only."""
    return [
        json.loads(send["snapshot"])
        for send in journal.sends(scope)
        if send["operation_id"] == operation.id
        and send["attempt"] == operation.attempt_count
        and send["snapshot"] is not None
    ]


def pending_echo(core, journal, handle, scope, task, snapshot):
    matching = next(
        (
            operation
            for operation in core.sync.list_operations(handle)
            if operation.task_id == task.id
            and not operation.fenced_by
            and operation.state in {"pending", "leased", "uncertain"}
            and operation.payload["desired_status"] == snapshot.status
            and snapshot.assignee_id == scope.principal
            and snapshot.title == task.title
            and snapshot.body == task.body
        ),
        None,
    )
    # Status/prose alone cannot attribute a newer human revision to our write.
    # Only the retained response binds an exact snapshot to this operation and
    # attempt. An unresolved response remains unknown, never a proven echo.
    responses = retained_responses(journal, scope, matching) if matching else []
    return matching, responses, asdict(snapshot) in responses
