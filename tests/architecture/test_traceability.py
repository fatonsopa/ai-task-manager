"""Focused metadata controls; these do not execute product acceptance."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "traceability_handoff", ROOT / "tools/check_handoff.py"
)
HANDOFF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HANDOFF)


def load(path):
    return json.loads((ROOT / path).read_text())


def traceability_failures(change=None):
    crosswalk = load("config/traceability.json")
    work = load("config/work-items.json")
    if change:
        change(crosswalk, work)
    failures = []
    HANDOFF.verify_traceability(
        crosswalk,
        load("config/requirements.json")["requirements"],
        load("tests/acceptance/cases.json")["cases"],
        work,
        lambda condition, message: failures.append(message) if not condition else None,
    )
    return failures


def test_complete_current_ownership_is_valid_without_acceptance_execution():
    assert not traceability_failures()


@pytest.mark.parametrize(
    "change",
    [
        lambda c, w: c["requirements"].pop(),
        lambda c, w: c["cases"].pop(),
        lambda c, w: c["cases"].append(copy.deepcopy(c["cases"][0])),
        lambda c, w: c["cases"][0].update(id="unknown-case"),
        lambda c, w: c["requirements"][0].update(id="R99"),
        lambda c, w: c["cases"][0]["implementation_owners"][0].update(task="T99"),
        lambda c, w: c["cases"][0]["implementation_owners"][0].update(phase="P99"),
        lambda c, w: c["cases"][0]["implementation_owners"][0].update(phase="P10"),
        lambda c, w: c["cases"][0].update(implementation_owners=[]),
        lambda c, w: c["cases"][0].update(verification_owners=[]),
        lambda c, w: c["cases"][0].update(rationale="  "),
        lambda c, w: c["cases"][0].update(supporting_owners=None),
        lambda c, w: c["cases"][0].update(implementation_owners=[None]),
        lambda c, w: c["cases"][0]["implementation_owners"][0].update(task=[]),
        lambda c, w: c.update(schema_version=True),
        lambda c, w: c.update(cases={}),
        lambda c, w: c["cases"][0]["supporting_owners"].append(
            copy.deepcopy(c["cases"][0]["implementation_owners"][0])
        ),
        lambda c, w: next(t for t in w["tasks"] if t["id"] == "T06")[
            "requirements"
        ].remove("R11"),
    ],
    ids=[
        "missing-requirement", "missing-case", "duplicate-case", "unknown-case",
        "unknown-requirement", "unknown-task", "unknown-phase", "wrong-task-phase",
        "missing-implementation", "missing-verification", "missing-rationale",
        "malformed-support", "malformed-owner", "nonstring-task", "bool-schema",
        "malformed-cases", "roles-overlap", "unassigned-R11",
    ],
)
def test_incomplete_or_invalid_ownership_is_rejected(change):
    assert traceability_failures(change)


def frozen_policy():
    policy = load("config/execution-policy.json")
    policy["integration"]["merge_interface"].update(
        status="contract_frozen",
        contract_ref="docs/spec_driven_development.md#code-and-merge-plan-selection-contract",
        public_operations=["workflow_run", "workflow_resume"],
        cli_actions=[
            "aitm workflow run --input request.json",
            "aitm workflow resume --input request.json",
        ],
        implementation_status="installed_code_merge_composition_and_acceptance_pending",
    )
    return policy


def integration_failures(policy):
    failures = []
    HANDOFF.verify_standalone_integration(
        policy, lambda condition, message: failures.append(message) if not condition else None
    )
    return failures


def test_frozen_contract_declares_existing_routes_without_implementation_credit():
    assert not integration_failures(frozen_policy())


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_ref", ""),
        ("contract_ref", "docs/unknown.md#invented"),
        ("public_operations", ["merge-to-dev"]),
        ("cli_actions", ["aitm merge-to-dev"]),
        ("implementation_status", "implemented"),
        ("command_name", "merge-to-dev"),
        ("compatibility_wrappers_allowed", True),
        ("status", "unknown"),
    ],
)
def test_frozen_contract_cannot_invent_routes_or_claim_implementation(field, value):
    policy = frozen_policy()
    policy["integration"]["merge_interface"][field] = value
    assert integration_failures(policy)
