"""Independent operation expectations for the three reviewed composite boundaries."""

import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Frozen from destination policy/provider confirmation, atomic admission/resource
# contracts, and installation/worker/merge lifetime responsibilities. These are
# deliberate expectations, not inferred from the crosswalk or requirement joins.
EXPECTED_STEPS = {
    "WF-delivery-valid": (5, 6, 7),
    "WF-delivery-omit-05": (5,),
    "WF-delivery-omit-06": (6,),
    "WF-delivery-omit-07": (7,),
    "WF-delivery-order-04": (5,),
    "WF-delivery-order-05": (5, 6),
    "WF-delivery-order-06": (6, 7),
    "WF-task-loop-valid": (6,),
    "WF-task-loop-omit-06": (6,),
    "WF-task-loop-order-05": (6,),
    "WF-task-loop-order-06": (6,),
    "WF-removal-valid": (2,),
    "WF-removal-omit-02": (2,),
    "WF-removal-order-01": (2,),
    "WF-removal-order-02": (2,),
}
EXPECTED_OWNERS = {
    "delivery": ({"T05", "T10"}, {"T09"}),
    "task-loop": ({"T06", "T07"}, set()),
    "removal": ({"T23", "T08", "T09"}, {"T07"}),
}


def current_data():
    crosswalk = json.loads((ROOT / "config/traceability.json").read_text())
    work = json.loads((ROOT / "config/work-items.json").read_text())
    return {row["id"]: row for row in crosswalk["cases"]}, {
        task["id"]: task["phase"] for task in work["tasks"]
    }


def semantic_failures(rows, phases):
    failures = []
    for identifier, steps in EXPECTED_STEPS.items():
        row = rows[identifier]
        group = identifier[3:].rsplit("-valid", 1)[0] if identifier.endswith("-valid") else (
            identifier[3:].rsplit("-omit-", 1)[0]
            if "-omit-" in identifier else identifier[3:].rsplit("-order-", 1)[0]
        )
        implementing, supporting = EXPECTED_OWNERS[group]
        bindings = row.get("step_owners", [])
        if [binding.get("step") for binding in bindings] != list(steps):
            failures.append((identifier, "missing/duplicate/foreign operation binding"))
        row_impl = {owner["task"] for owner in row["implementation_owners"]}
        row_support = {owner["task"] for owner in row["supporting_owners"]}
        for binding in bindings:
            seen = set()
            for role, expected in (
                ("implementation_owners", implementing), ("supporting_owners", supporting)
            ):
                owners = binding.get(role, [])
                actual = {owner.get("task") for owner in owners}
                if actual != expected or len(actual) != len(owners):
                    failures.append((identifier, binding.get("step"), role, "wrong owners"))
                for owner in owners:
                    task = owner.get("task")
                    if task not in phases or owner.get("phase") != phases.get(task):
                        failures.append((identifier, "unknown owner or wrong phase"))
                    if task in seen:
                        failures.append((identifier, "overlapping operation roles"))
                    seen.add(task)
                allowed = row_impl if role == "implementation_owners" else row_impl | row_support
                if not actual <= allowed:
                    failures.append((identifier, "operation owner absent from aggregate role"))
    return failures


def test_reviewed_composite_operations_have_complete_consistent_accountability():
    assert not semantic_failures(*current_data())


@pytest.mark.parametrize(
    "identifier,role,task",
    [
        (identifier, role, task)
        for identifier in EXPECTED_STEPS
        for group, roles in EXPECTED_OWNERS.items()
        if identifier.startswith("WF-" + group + "-")
        for role, tasks in zip(("implementation_owners", "supporting_owners"), roles)
        for task in sorted(tasks)
    ],
)
def test_each_accountable_operation_owner_is_required_even_if_listed_elsewhere(
    identifier, role, task
):
    rows, phases = current_data()
    # Keep aggregate ownership intact: an owner on another step cannot conceal
    # the missing responsibility at this exact operation boundary.
    binding = rows[identifier]["step_owners"][0]
    binding[role] = [owner for owner in binding[role] if owner["task"] != task]
    assert semantic_failures(rows, phases)


@pytest.mark.parametrize("group,old_owner", [("delivery", "T09"), ("task-loop", "T07"),
                                            ("removal", "T07")])
def test_original_offending_operation_assignment_is_rejected(group, old_owner):
    rows, phases = current_data()
    for identifier in EXPECTED_STEPS:
        if identifier.startswith("WF-" + group + "-"):
            for binding in rows[identifier]["step_owners"]:
                binding["implementation_owners"] = [{"task": old_owner,
                                                     "phase": phases[old_owner]}]
                binding["supporting_owners"] = []
    assert semantic_failures(rows, phases)


@pytest.mark.parametrize("fault", ["unknown-task", "wrong-phase", "overlap", "foreign-step"])
def test_new_operation_bindings_reject_reference_phase_and_role_drift(fault):
    rows, phases = current_data()
    binding = rows["WF-task-loop-valid"]["step_owners"][0]
    if fault == "unknown-task":
        binding["implementation_owners"][0]["task"] = "T99"
    elif fault == "wrong-phase":
        binding["implementation_owners"][0]["phase"] = "P10"
    elif fault == "overlap":
        binding["supporting_owners"] = [copy.deepcopy(binding["implementation_owners"][0])]
    else:
        binding["step"] = 99
    assert semantic_failures(rows, phases)
