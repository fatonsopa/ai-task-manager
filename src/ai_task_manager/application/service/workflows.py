"""The shared public workflow handlers select only installed trusted references."""


def workflow_run(service, data, invocation):
    return service._required("workflows").run(**data)


def workflow_resume(service, data, invocation):
    return service._required("workflows").resume(**data)


def workflow_inspect(service, data, invocation):
    from ai_task_manager.application.workflows.inspection import current_grant

    current_grant(service)
    envelope = service._required("workflows").inspect(**data)
    current_grant(service)
    return envelope
