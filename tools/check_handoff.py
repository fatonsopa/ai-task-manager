#!/usr/bin/env python3
"""Check this planning package. This does not execute product acceptance tests."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


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
    verify_execution_limits(policy, require)
    require(
        set(policy.get("acceptance", {}).get("required_hosts", [])) == {"claude-code", "codex-cli"},
        "Both hosts are required.",
    )
    integration = policy.get("integration", {})
    merge_gate = integration.get("merge_refactor_gate", {})
    require(
        integration.get("merge_script_changes_allowed") is True
        and merge_gate.get("authorization") == "conditional_user_direction_2026-11-01"
        and all(
            merge_gate.get(key) is True
            for key in (
                "latest_source_audit_required",
                "demonstrated_incompatibility_required",
                "regression_cases_frozen_before_changes",
                "commands_and_intent_preserved",
                "disposable_copy_verification_required",
                "clean_cutover_and_recovery_required",
            )
        )
        and merge_gate.get("production_or_normal_session_activation_authorized") is False,
        "Merge changes require the recorded audit-first exception and preserved command intent.",
    )
    require(
        policy.get("linear", {}).get("team_wide_start_cleanup_allowed") is False,
        "Team-wide cleanup is forbidden.",
    )
    require(
        policy.get("repository", {}).get("copyright_holder") == "Manaferra Inc.",
        "Copyright holder mismatch.",
    )
    active = policy.get("active_host_profiles", {}).get("claude-code", {})
    require(
        active.get("label") == "claude_roni" and active.get("default_account_allowed") is False,
        "Claude launches must select the approved profile and forbid default-account fallback.",
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
