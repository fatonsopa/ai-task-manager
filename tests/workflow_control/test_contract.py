"""Additive public schemas and missing integration behavior retain old authority."""

import json
from pathlib import Path

import pytest

from ai_task_manager.application.service.handlers import HANDLERS
from ai_task_manager.application.service.models import SCHEMAS
from tests.application.helpers import ServiceRig


def test_WE03_preserve_all_original_schemas_and_handlers():
    prior = json.loads((Path(__file__).parent / "frozen/old_operations_v1.json").read_text())
    assert {k: v for k, v in SCHEMAS.items() if k in prior} == prior
    assert set(SCHEMAS) - set(prior) == {"workflow_run", "workflow_resume"}
    assert set(HANDLERS) == set(SCHEMAS)
    assert not any("merge-to-dev" in name or "passed" in name for name in SCHEMAS)


@pytest.mark.parametrize(
    "operation,data",
    [
        ("workflow_run", {"task_id": "task", "plan_ref": "plan", "operation_key": "key"}),
        ("workflow_resume", {"workflow_ref": "ref"}),
    ],
)
def test_WE10_missing_owner_is_explicit_blocked(tmp_path, operation, data):
    service = ServiceRig(tmp_path).service
    result = service.call(operation, data)
    assert result["exit_code"] == 4 and result["outcome"] == "blocked"
    assert "workflows integration is not configured" in result["error"]["message"]


@pytest.mark.parametrize("missing", ["runtime", "repository", "provider"])
def test_WE10_required_composition_refuses_before_start(tmp_path, missing):
    from ai_task_manager.application.service.models import Unavailable
    from ai_task_manager.application.workflows.service import WorkflowService
    from tests.workflows.helpers import WorkflowFixture

    fixture = WorkflowFixture(tmp_path)
    try:
        setattr(fixture.driver, missing, None)
        service = WorkflowService(fixture.driver)
        with pytest.raises(Unavailable):
            service.run(fixture.task.id, fixture.plan.id, "new-key")
        assert not fixture.runtime.dispatches
    finally:
        fixture.close()
