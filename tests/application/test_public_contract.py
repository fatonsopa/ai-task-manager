"""Independent public-surface and authority boundary expectations."""

import pytest

from ai_task_manager.application.service.handlers import HANDLERS
from ai_task_manager.application.service.models import SCHEMAS, validate
from ai_task_manager.domains.work.errors import ValidationError


def test_public_contract_has_required_operations_and_no_authority_issuer():
    required = {
        "workflow_run",
        "workflow_resume",
        "session_show",
        "session_resume",
        "writing_prepare",
        "writing_review",
        "writing_finalize",
        "task_submit",
        "task_find",
        "task_inspect",
        "task_admit",
        "task_select",
        "task_list",
        "task_show",
        "task_action",
        "task_revise",
        "task_progress",
        "worker_start",
        "worker_execute",
        "event_list",
        "event_acknowledge",
        "sync_now",
        "task_external_changes",
        "task_accept_external_prose",
        "task_settle_external",
        "task_decision_prepare",
        "task_decision_request",
        "task_decision_show",
        "task_decision_resolve",
    }
    assert set(SCHEMAS) == required == set(HANDLERS)
    for schema in SCHEMAS.values():
        assert schema["additionalProperties"] is False
        assert (
            not {"human", "reviewed", "principal", "scope", "startup", "status", "ready"}
            & schema["properties"].keys()
        )
    assert set(SCHEMAS["task_submit"]["properties"]) == {
        "writing_ref",
        "operation_key",
        "parent_task_id",
        "followup_ref",
    }
    assert "command_ref" in SCHEMAS["worker_execute"]["required"]
    assert not {"command", "argv", "environment"} & SCHEMAS["worker_execute"]["properties"].keys()
    assert "observation_ref" in SCHEMAS["event_acknowledge"]["required"]
    with pytest.raises(ValidationError, match="Unknown application operation"):
        validate("task_decision_approve", {})
