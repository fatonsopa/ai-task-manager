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
