def record_scope_removal(core, journal, handle, task, metadata, operation_key):
    _, scope = core.session_binding(handle)
    ref = journal.scope_removal_receipt(scope, task, metadata)
    return core.external.observe(
        handle, task.id, task.version, task.decision_revision, ref, operation_key
    )
