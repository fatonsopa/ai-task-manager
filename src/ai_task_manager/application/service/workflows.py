"""The shared public workflow handlers select only installed trusted references."""


def workflow_run(service, data, invocation):
    return service._required("workflows").run(**data)


def workflow_resume(service, data, invocation):
    return service._required("workflows").resume(**data)
