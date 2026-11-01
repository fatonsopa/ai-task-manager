import json
from pathlib import Path

from ai_task_manager.application.service.handlers import HANDLERS
from ai_task_manager.application.service.models import SCHEMAS


def test_ea63_old_schemas_are_unchanged_and_only_three_reviewed_operations_are_added():
    root = Path(__file__).with_name("frozen")
    legacy = json.loads((root / "legacy-schemas.json").read_text())["schemas"]
    contract = json.loads((root / "admission-contract-v1.json").read_text())
    added = contract["public_operations"]
    assert set(SCHEMAS) == set(HANDLERS) == set(legacy) | set(added)
    assert {name: SCHEMAS[name] for name in legacy} == legacy
    assert {name: SCHEMAS[name] for name in added} == {
        name: operation["input"] for name, operation in added.items()
    }
