import json
from pathlib import Path

from ai_task_manager.application.service.handlers import HANDLERS
from ai_task_manager.application.service.models import SCHEMAS


def test_ea63_old_schemas_are_unchanged_with_explicit_later_additions():
    root = Path(__file__).with_name("frozen")
    legacy = json.loads((root / "legacy-schemas.json").read_text())["schemas"]
    contract = json.loads((root / "admission-contract-v1.json").read_text())
    added = contract["public_operations"]
    # The accepted delivery baseline also has the shared workflow operations;
    # DP04 adds selection, external settlement and decision requests. Keep the
    # accepted older input schemas exact and enumerate each later operation.
    later = {
        "workflow_run",
        "workflow_resume",
        "task_select",
        "task_settle_external",
        "task_decision_prepare",
        "task_decision_request",
        "task_decision_show",
        "task_decision_resolve",
    }
    assert set(SCHEMAS) == set(HANDLERS) == set(legacy) | set(added) | later
    assert {name: SCHEMAS[name] for name in legacy} == legacy
    assert {name: SCHEMAS[name] for name in added} == {
        name: operation["input"] for name, operation in added.items()
    }
    assert SCHEMAS["task_select"] == {
        "type": "object",
        "properties": {
            "discovery_ref": {"type": "string", "minLength": 1, "maxLength": 256},
            "operation_key": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "required": ["discovery_ref", "operation_key"],
        "additionalProperties": False,
    }
