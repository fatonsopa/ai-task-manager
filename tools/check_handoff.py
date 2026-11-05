#!/usr/bin/env python3
"""Check this planning package. This does not execute product acceptance tests."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def verify_standalone_integration(policy, require):
    """Validate declarations; this is not runtime or source-tree attestation."""
    integration = policy.get("integration", {})
    if not isinstance(integration, dict):
        require(False, "Integration must be an object.")
        return
    interface = integration.get("merge_interface", {})
    gate = integration.get("merge_refactor_gate", {})
    target = integration.get("target_cutover", {})
    if not all(isinstance(item, dict) for item in (interface, gate, target)):
        require(False, "Interface, merge gate and target cutover must be objects.")
        return
    require(
        integration.get("revision") == 2 and integration.get("architecture") == "standalone",
        "Integration must use the current standalone contract.",
    )
    retired_settings = {"merge_script", "compatibility_entrypoints", "task_management_document"}
    require(
        not retired_settings & integration.keys()
        and interface.get("retired_entrypoints") == ["scripts/merge-to-dev.sh"]
        and interface.get("compatibility_wrappers_allowed") is False,
        "The retired merge script must have no active entrypoint, wrapper "
        "or required legacy guide.",
    )
    require(
        interface.get("owner") == "standalone_product_contract"
        and interface.get("status") in {"contract_pending", "contract_frozen", "implemented"}
        and (interface.get("status") != "contract_pending" or interface.get("command_name") is None)
        and (
            interface.get("status") not in {"contract_frozen", "implemented"}
            or (
                isinstance(interface.get("contract_ref"), str)
                and bool(interface["contract_ref"].strip())
            )
        ),
        "A pending merge contract cannot invent an implemented public command.",
    )
    if interface.get("status") == "contract_frozen":
        require(
            interface.get("command_name") is None
            and interface.get("contract_ref")
            == "docs/spec_driven_development.md#code-and-merge-plan-selection-contract"
            and interface.get("public_operations") == ["workflow_run", "workflow_resume"]
            and interface.get("cli_actions") == [
                "aitm workflow run --input request.json",
                "aitm workflow resume --input request.json",
            ]
            and interface.get("implementation_status")
            == "installed_code_merge_composition_and_acceptance_pending",
            "A frozen merge contract needs the known workflow routes/reference "
            "and must retain pending implementation/acceptance.",
        )
    require(
        integration.get("copied_application_scripts_allowed") is False
        and integration.get("original_application_access") == "read_only_scenario_analysis"
        and integration.get("historical_source_storage") == "outside_product_repository"
        and integration.get("legacy_instruction_paths_required") is False
        and integration.get("preserve_required_state_and_history") is True,
        "Source scripts stay outside the product; original files and required history "
        "remain protected.",
    )
    require(
        all(target.get(key) is True for key in (
            "after_standalone_acceptance", "requires_user_request",
            "fresh_all_file_and_markdown_audit", "remove_superseded_whole_files_or_overlap",
        )) and target.get("dual_writers_allowed") is False,
        "Target cutover needs the later request, fresh audit and granular removal "
        "without dual writers.",
    )
    require(
        gate.get("authorization") == "explicit_user_standalone_direction_2026-11-02"
        and all(gate.get(key) is True for key in (
            "scenario_audit_required", "regression_cases_frozen_before_changes",
            "independently_authored_projects_required", "clean_cutover_and_recovery_required",
        ))
        and gate.get("original_application_mutation_allowed") is False
        and gate.get("production_or_normal_session_activation_authorized") is False
        and "demonstrated_incompatibility_required" not in gate,
        "Merge replacement needs reviewed standalone scenarios and safe recovery, "
        "without legacy compatibility restrictions.",
    )


def verify_traceability(traceability, requirements, cases, work, require):
    """Check ownership references and completeness, not semantic review or acceptance."""
    if not isinstance(traceability, dict):
        require(False, "Traceability must be an object.")
        return
    require(
        type(traceability.get("schema_version")) is int
        and traceability.get("schema_version") == 1,
        "Traceability needs schema version 1.",
    )
    require(
        isinstance(traceability.get("scope"), str)
        and bool(traceability["scope"].strip()),
        "Traceability needs an explicit scope.",
    )
    tasks = {row["id"]: row for row in work.get("tasks", [])}
    phases = {row["id"] for row in work.get("phases", [])}
    required = {row["id"] for row in requirements}
    tagged = {identifier for row in tasks.values() for identifier in row.get("requirements", [])}
    require(required <= tagged, "Requirements without a declared work-item owner.")
    for task in tasks.values():
        require(task.get("phase") in phases, f"{task['id']}: unknown task phase.")
    for group, identifiers in (("requirements", required), ("cases", {c["id"] for c in cases})):
        rows = traceability.get(group)
        if not isinstance(rows, list):
            require(False, f"Traceability {group} must be a list.")
            continue
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                require(False, f"Traceability {group}: malformed row.")
                continue
            identifier = row["id"]
            require(identifier not in seen, f"Traceability {group}: duplicate {identifier}.")
            seen.add(identifier)
            require(identifier in identifiers, f"Traceability {group}: unknown {identifier}.")
            require(
                isinstance(row.get("rationale"), str) and bool(row["rationale"].strip()),
                f"{identifier}: ownership needs a behavior rationale.",
            )
            assigned = set()
            for role in ("implementation_owners", "supporting_owners", "verification_owners"):
                owners = row.get(role)
                if not isinstance(owners, list):
                    require(False, f"{identifier}: {role} must be a list.")
                    continue
                if role != "supporting_owners":
                    require(bool(owners), f"{identifier}: missing {role}.")
                for owner in owners:
                    if not isinstance(owner, dict) or set(owner) != {"task", "phase"}:
                        require(False, f"{identifier}: malformed {role} owner.")
                        continue
                    task_id, phase = owner["task"], owner["phase"]
                    if not isinstance(task_id, str) or not isinstance(phase, str):
                        require(False, f"{identifier}: owner task and phase must be strings.")
                        continue
                    require(task_id not in assigned, f"{identifier}: repeated owner {task_id}.")
                    assigned.add(task_id)
                    require(task_id in tasks, f"{identifier}: unknown owner {task_id}.")
                    require(phase in phases, f"{identifier}: unknown owner phase {phase}.")
                    if task_id in tasks:
                        require(
                            phase == tasks[task_id].get("phase"),
                            f"{identifier}: {task_id} phase differs from the work graph.",
                        )
        require(seen == identifiers, f"Traceability {group}: incomplete identifier coverage.")


def verify_execution_limits(policy, require):
    limits = policy.get("limits", {})
    for key in ("max_elapsed_seconds", "max_incremental_spend_usd", "max_total_retry_rounds"):
        require(
            key in limits and limits[key] is None,
            f"{key}: the recorded authorization lifts this total ceiling explicitly.",
        )
    for key in (
        "resolved_worker_cap_must_be_finite",
        "paid_call_requires_cost_reservation",
        "per_invocation_timeout_required",
        "reviewed_finite_batch_required",
        "continuation_requires_evidence_and_checkpoint",
    ):
        require(limits.get(key) is True, f"{key}: finite execution controls remain required.")
    require(
        limits.get("unknown_paid_cost_action") == "block_paid_call",
        "Unknown new paid pricing must still refuse admission.",
    )
    require(
        limits.get("legacy_defaults_without_superseding_authorization")
        == {
            "max_elapsed_seconds": 172800,
            "max_incremental_spend_usd": 100,
            "stop_admission_before_deadline_seconds": 1800,
        },
        "Unamended bounded defaults must remain explicit.",
    )
    authority = policy.get("limits_authorization", {})
    require(
        authority.get("id") == "USER-UNTIL-PROVEN-20261101T230813Z"
        and authority.get("record_sha256")
        == "26031e25d7dc3fc4f91f52f7023ed9ae1c6534ce72e227bf562157b379fd9c68"
        and authority.get("cryptographic_origin_proof") is False,
        "Lifted totals require the exact recorded user source with honest provenance.",
    )
    acceptance = policy.get("acceptance", {})
    profiles = acceptance.get("required_profiles", [])
    expected = {
        ("claude-code", "claude-opus-5-5", "high"),
        ("claude-code", "claude-sonnet-5-5", "high"),
        ("codex-cli", "gpt-6.1-sol", "high"),
        ("codex-cli", "gpt-6-astra", "high"),
    }
    actual = {
        (p.get("host"), p.get("model"), p.get("reasoning_effort"))
        for p in profiles
        if isinstance(p, dict)
    }
    require(
        len(profiles) == len(actual) == 4 and actual == expected,
        "All four selected host/model/effort profiles must remain separately required.",
    )
    require(
        acceptance.get("minimum_models_per_host") == 2
        and acceptance.get("excluded_model_family") == "Fable"
        and acceptance.get("each_required_profile_needs_complete_separate_evidence") is True,
        "Require at least two non-Fable models per CLI without mixed-profile aggregation.",
    )
    require(
        all(p.get("execution_status") == "not_run" for p in profiles if isinstance(p, dict)),
        "Planning profile cells cannot claim actual acceptance execution.",
    )


def build_manifest(root):
    """Return the deterministic documentation/support index; never write it here."""
    root = Path(root).absolute()
    layout = json.loads((root / "config/repository-layout.json").read_text())
    names = {
        str(path.relative_to(root))
        for path in (root / "docs").rglob("*")
        if path.is_file() and path.suffix.lower() in layout["document_suffixes"]
    }
    names.update(layout["authorities"].values())
    names.update(row["new"] for row in layout["relocations"])
    names.update(layout["current_consumers"])
    names.update(
        {
            "CLAUDE.md",
            "config/repository-layout.json",
            "config/traceability.json",
            "tools/repository_layout.py",
            "tests/architecture/test_repository_layout.py",
        }
    )
    names.discard("config/documentation-manifest.json")
    previous = next(
        row["before_sha256"] for row in layout["relocations"] if row["old"] == "docs/MANIFEST.json"
    )
    return {
        "schema_version": 3,
        "scope": (
            "Documentation and declared supporting inputs; "
            "not whole-product or runtime attestation."
        ),
        "path_base": "repository_root",
        "previous_manifest_sha256": previous,
        "files": [
            {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
            for name in sorted(names)
        ],
    }


def verify(root):
    # --root continues to accept the former documentation-package directory.
    # Current data and tools have repository owners, so resolve both entry forms.
    root = Path(root).absolute()
    if (root / "assets/diagrams").is_dir() and (root.parent / "config").is_dir():
        root = root.parent
    documents = root / "docs"
    failures = []

    def require(condition, message):
        if not condition:
            failures.append(message)

    def read(name):
        try:
            return json.loads((root / name).read_text())
        except (OSError, ValueError) as error:
            failures.append(f"{name}: {error}")
            return {}

    policy = read("config/execution-policy.json")
    requirement_data = read("config/requirements.json")
    acceptance = read("tests/acceptance/cases.json")
    work = read("config/work-items.json")
    traceability = read("config/traceability.json")
    requirements = requirement_data.get("requirements", [])
    req_ids = {r["id"] for r in requirements}
    cases = acceptance.get("cases", [])
    case_ids = {c["id"] for c in cases}
    workflows = acceptance.get("workflows", [])
    require(
        len(req_ids) == len(requirements) == 38,
        "Expected 38 unique requirements in the maintained specification.",
    )
    require(len(case_ids) == len(cases) == 248, "Expected 248 unique authored acceptance cases.")
    covered = set()
    behavioral = set()
    for case in cases:
        require(set(case["requirements"]) <= req_ids, f"{case['id']}: unknown requirement.")
        require(
            bool(case.get("actions")) and bool(case.get("expected")),
            f"{case['id']}: missing procedure or oracle.",
        )
        require(
            case.get("execution_status") == "not_run",
            f"{case['id']}: a planning case cannot claim executed results.",
        )
        covered.update(case["requirements"])
        if case["layer"] == "behavior":
            behavioral.update(case.get("workflow_ids", []))
            require(
                set(case["hosts"]) == {"claude-code", "codex-cli"},
                f"{case['id']}: missing required host.",
            )
    require(covered == req_ids, "Requirements without acceptance coverage.")
    for workflow in workflows:
        wid = workflow["id"]
        require(f"WF-{wid}-valid" in case_ids, f"{wid}: missing valid-path case.")
        require(wid in behavioral, f"{wid}: missing behavioral workflow coverage.")
        for index in range(len(workflow["steps"])):
            require(
                f"WF-{wid}-omit-{index + 1:02}" in case_ids, f"{wid}: missing omitted-step test."
            )
        for index in range(len(workflow["steps"]) - 1):
            require(
                f"WF-{wid}-order-{index + 1:02}" in case_ids, f"{wid}: missing adjacent-order test."
            )
    for group in ("phases", "tasks"):
        rows = work.get(group, [])
        table = {r["id"]: r for r in rows}
        require(len(table) == len(rows), f"{group}: duplicate identifiers.")
        visited, active = set(), set()

        def walk(identifier):
            if identifier in active:
                failures.append(f"{group}: dependency cycle at {identifier}.")
                return
            if identifier in visited:
                return
            active.add(identifier)
            for dependency in table[identifier].get("depends_on", []):
                if dependency not in table:
                    failures.append(f"{identifier}: missing dependency {dependency}.")
                else:
                    walk(dependency)
            active.remove(identifier)
            visited.add(identifier)

        for identifier in table:
            walk(identifier)
        for row in rows:
            require(
                set(row.get("requirements", [])) <= req_ids, f"{row['id']}: unknown requirement."
            )
            require(
                row.get("status")
                in {"not_started", "in_progress", "component_verified", "complete", "blocked"},
                f"{row['id']}: unknown progress status.",
            )
            if row.get("status") != "not_started":
                require(
                    isinstance(row.get("evidence"), str) and bool(row["evidence"].strip()),
                    f"{row['id']}: progress needs explicit evidence and scope.",
                )
    verify_traceability(traceability, requirements, cases, work, require)
    verify_execution_limits(policy, require)
    require(
        set(policy.get("acceptance", {}).get("required_hosts", [])) == {"claude-code", "codex-cli"},
        "Both hosts are required.",
    )
    verify_standalone_integration(policy, require)
    require(
        policy.get("linear", {}).get("team_wide_start_cleanup_allowed") is False,
        "Team-wide cleanup is forbidden.",
    )
    require(
        policy.get("repository", {}).get("copyright_holder") == "Manaferra Inc.",
        "Copyright holder mismatch.",
    )
    active = policy.get("active_host_profiles", {}).get("claude-code", {})
    selected_fields = (
        "label", "executable", "config_dir_relative_to_home",
        "default_account_allowed", "fallback_allowed",
    )
    selections = [row.get("host_profile") for row in policy.get("user_decisions", [])
                  if row.get("id") == active.get("authorization_decision")]
    require(
        len(selections) == 1
        and selections[0] == {key: active.get(key) for key in selected_fields}
        and active.get("executable") == "claude"
        and active.get("fallback_allowed") is False,
        "Claude launches must match the explicitly approved account selection and forbid fallback.",
    )
    statuses = set(policy.get("linear", {}).get("workflow_statuses", []))
    review_rule = policy.get("linear", {}).get("review_exit", {})
    require(
        bool(statuses) and set(review_rule.get("allowed_destinations", [])) == statuses,
        "In Review must allow every configured destination.",
    )
    require(
        review_rule.get("requires_authorized_decision") is True
        and review_rule.get("requires_destination_evidence") is True,
        "Review exits must retain authorization and evidence guards.",
    )
    review_case = next((case for case in cases if case["id"] == "C04"), {})
    require(
        set(review_case.get("review_exit_targets", [])) == statuses,
        "C04 must cover every configured In Review destination.",
    )
    public_prose = list(documents.rglob("*.md")) + [
        root / name for name in ("README.md", "AGENTS.md", "CLAUDE.md")
    ]
    for path in public_prose:
        text = path.read_text()
        require(
            not re.search(r"(?i)\bfaton\b|/Users/[^$\s]+|/home/[^$\s]+", text),
            f"{path.name}: personal name or machine-specific user path.",
        )
        require(
            not re.search(r"lin_api_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{16,}", text),
            f"{path.name}: possible credential.",
        )
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            if target:
                require(
                    (path.parent / target).is_file(), f"{path.name}: broken local link {target}."
                )
    diagrams = documents / "assets/diagrams"
    require(len(list(diagrams.glob("*.mmd"))) == 11, "Expected eleven editable diagram sources.")
    require(len(list(diagrams.glob("*.svg"))) == 11, "Expected eleven rendered diagrams.")
    for source in diagrams.glob("*.mmd"):
        require(source.with_suffix(".svg").is_file(), f"{source.name}: missing SVG export.")
    manifest = root / "config/documentation-manifest.json"
    if manifest.exists():
        for item in read("config/documentation-manifest.json").get("files", []):
            candidate = root / item["path"]
            require(candidate.is_file(), f"Missing manifest file {item['path']}.")
            if candidate.is_file():
                require(
                    hashlib.sha256(candidate.read_bytes()).hexdigest() == item["sha256"],
                    f"Changed manifest file {item['path']}.",
                )
    return {
        "check": "planning_package_integrity",
        "passed": not failures,
        "findings": failures,
        "counts": {
            "requirements": len(requirements),
            "workflows": len(workflows),
            "cases": len(cases),
            "phases": len(work.get("phases", [])),
            "tasks": len(work.get("tasks", [])),
        },
        "product_acceptance_tests_executed": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    arguments = parser.parse_args()
    result = verify(arguments.root)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
