"""Current declarations reject the retired workflow without rewriting history."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("standalone_handoff", ROOT / "tools/check_handoff.py")
HANDOFF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HANDOFF)


def valid_policy():
    return json.loads((ROOT / "config/execution-policy.json").read_text())


def failures(policy):
    result = []

    def require(condition, message):
        if not condition:
            result.append(message)

    HANDOFF.verify_standalone_integration(policy, require)
    return result


def test_pending_standalone_contract_and_retained_history_are_valid():
    policy = valid_policy()
    assert policy["integration"]["merge_interface"]["command_name"] is None
    assert any("previous_integration" in item for item in policy["revision_history"])
    assert not failures(policy)


def test_old_audit_first_compatibility_route_is_rejected():
    policy = valid_policy()
    policy["integration"] = {
        "merge_script": "scripts/merge-to-dev.sh",
        "compatibility_entrypoints": "keep old command flow",
        "merge_refactor_gate": {"demonstrated_incompatibility_required": True},
    }
    assert failures(policy)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("merge_script",), "scripts/merge-to-dev.sh"),
        (("task_management_document",), ".claude/standards/task-management.md"),
        (("merge_interface", "compatibility_wrappers_allowed"), True),
        (("merge_interface", "command_name"), "invented-command"),
        (("copied_application_scripts_allowed",), True),
        (("historical_source_storage",), "inside_product_repository"),
        (("original_application_access",), "mutable"),
        (("legacy_instruction_paths_required",), True),
        (("target_cutover", "requires_user_request"), False),
        (("target_cutover", "dual_writers_allowed"), True),
        (("merge_refactor_gate", "demonstrated_incompatibility_required"), True),
        (("merge_refactor_gate", "production_or_normal_session_activation_authorized"), True),
    ],
)
def test_legacy_routing_copies_and_early_authority_cannot_be_enabled(path, value):
    policy = copy.deepcopy(valid_policy())
    selected = policy["integration"]
    for key in path[:-1]:
        selected = selected[key]
    selected[path[-1]] = value
    assert failures(policy)


@pytest.mark.parametrize("value", [None, [], "legacy-route"])
def test_malformed_integration_refuses_without_an_attribute_error(value):
    policy = valid_policy()
    policy["integration"] = value
    assert failures(policy)


def test_implemented_contract_requires_a_traceable_contract_reference():
    policy = valid_policy()
    policy["integration"]["merge_interface"]["status"] = "implemented"
    assert failures(policy)
