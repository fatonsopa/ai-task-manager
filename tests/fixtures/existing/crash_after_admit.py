"""A real process interruption after the installed handler's Core commit."""

import os
import runpy
from pathlib import Path

from ai_task_manager.application.service.facade import ApplicationService

original = ApplicationService._invoke


def interrupted(service, operation, data, invocation):
    result = original(service, operation, data, invocation)
    if operation == "task_admit" and result["ok"]:
        os._exit(86)
    return result


ApplicationService._invoke = interrupted
runpy.run_path(str(Path(__file__).with_name("installed_bootstrap.py")), run_name="__main__")
