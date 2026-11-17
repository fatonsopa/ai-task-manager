"""Normal installed owner and a separate service Core share declared queue facts."""

import json

import pytest

from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import EvidenceRequired
from tests.workflow_installation.test_installation import cli, installed  # noqa: F401


def test_normal_installed_plan_declaration_is_visible_to_separate_service_core(installed):  # noqa: F811
    root, package, _ = installed
    ready = json.loads((root / "prepared.json").read_text())
    reader = load_service(root / "service.json")
    task = reader.core.get_task(reader.handle, ready["task_id"])
    view = reader.core.scheduling.explain(reader.handle, task.id)
    assert view["selection_state"] == "ready", view
    # The independent reader can consume the validated projection, but cannot
    # reuse that historical fact as the live installer capability for a start.
    with pytest.raises(EvidenceRequired):
        reader.core.execution.declared_resources(reader.scope, task)
    public = cli(installed, "task", "show", {"task_id": task.id})
    result = json.loads(public.stdout or public.stderr)
    assert public.returncode == 0, result
    assert result["data"]["scheduling"]["selection_state"] == view["selection_state"]
