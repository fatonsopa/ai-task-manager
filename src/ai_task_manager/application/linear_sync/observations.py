from ai_task_manager.config.files import digest


def ownership_removal(snapshot):
    """Hash changed ownership only; never retain newly foreign issue content."""
    return {
        "id": snapshot.id,
        "updatedAt": snapshot.revision,
        "scopeDigest": digest(
            {
                "assignee": snapshot.assignee_id,
                "delegate": snapshot.delegate_id,
                "delegate_is_application": snapshot.delegate_is_application,
            }
        ),
    }


def record_scope_removal(
    core, journal, handle, task, metadata, operation_key, *, check_authority=None
):
    _, scope = core.session_binding(handle)
    ref = journal.scope_removal_receipt(scope, task, metadata)
    if check_authority is not None:
        check_authority()
    return core.external.observe(
        handle, task.id, task.version, task.decision_revision, ref, operation_key
    )
