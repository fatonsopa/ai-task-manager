"""Exercise declarations through public run and the actual local worker join."""


import pytest

from ai_task_manager.application.workflows.service import WorkflowService
from tests.workflows.helpers import WorkflowFixture


@pytest.mark.parametrize("complete", [False, True])
def test_public_run_consumes_trusted_plan_declarations_before_actual_worker_start(tmp_path, complete):
    wf = WorkflowFixture(tmp_path, resource_locks=() if complete else None)
    try:
        driver = wf.new_driver()
        service = WorkflowService(driver, {wf.task.id: wf.plan.id})
        result = service.run(wf.task.id, wf.plan.id, "workflow-intake")
        with wf.rig.db.transaction() as conn:
            count = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
        if complete:
            assert result["data"]["state"] == "awaiting_review", result
            assert count == len(wf.runtime.dispatches) == 1
            attempt = next(iter(wf.runtime.dispatches))
            assert (wf.runtime.pair(attempt).worktree / "result.txt").is_file()
        else:
            assert result["data"]["state"] == "blocked", result
            assert count == 0 and not wf.runtime.dispatches
    finally:
        wf.close()
