# AI task manager documentation

This directory is the maintained documentation for the standalone task coordinator being built for Claude Code and Codex CLI. Use these sources to continue implementation and judge readiness; earlier handoff downloads are historical. Product acceptance remains incomplete: current component results and blockers are in the Test Status Report (TSR), [test_status_report.md](test_status_report.md).

## Start

Work from the repository root with Git and Python 3 available. Paths in code spans are relative to that root. Read the Policy (POL), [policy.md](policy.md), then the Spec Driven Development document (SDD), [spec_driven_development.md](spec_driven_development.md), and the Implementation Plan (IP), [implementation_plan.md](implementation_plan.md). Inspect `config/execution-policy.json`, requirements, work graph and acceptance inventory before acting. Documents describe approved work; they do not authorize production actions. The repository address stays in `config/execution-policy.json` so personal identifiers do not need to appear in Markdown.

The user lifted the earlier total spending, elapsed-time and retry ceilings for this approved build. Preserve the original run and accounting, and use finite measured concurrency, reviewed batches and bounded invocations. Use the supplied public repository and the approved `claude_roni` profile; the original `claude` account remains unavailable. Use the current approved Technical Writer and Skill Builder, and audit affected work when the user supplies an update. Every task prose write needs the appropriate reference and Writing Style (WS) review. A missing second Linear identity blocks live two-user acceptance only; continue independent work.

The primary Linear key may be read from the application repository's `.env.local` as `LINEAR_API_KEY`, without copying or displaying it. Authentication or development-task writes do not establish product fixture acceptance. Verify actual scope before each live fixture run. Docker Desktop must be running for dependent application-database/Linux-container cases.

When a distinct second test identity is available, run this in an interactive terminal from the repository root:

```sh
python3 tools/setup_test_credentials.py --secondary-only
```

The helper uses hidden input and stores `LINEAR_TEST_USER_B_TOKEN` outside Git at `$HOME/.config/ai-task-manager/test-credentials.env`, mode 600. It must identify a different user with TST access. Do not paste tokens into chat or test logs.

## Check the result

Check the documentation package and cross-references from the repository root:

```sh
python3 tools/check_handoff.py
```

A successful check reports consistent requirements, cases, links and diagram pairs. Run `python3 tools/repository_layout.py` to check declared file ownership and runtime import boundaries. It does not execute product acceptance. The maintained counts remain 38 requirements, 248 authored cases and 14 workflows. Use the Test Plan (TP), [test_plan.md](test_plan.md), for actual test entry/exit criteria and evidence distinctions.

## Find your way around

| Need | Authoritative home |
| --- | --- |
| Approved principles, authority and roles | Policy (POL), [policy.md](policy.md) |
| Complete task/Linear behavior, guards and reporting | Spec Driven Development document (SDD), [spec_driven_development.md](spec_driven_development.md) |
| Approved sidebar interaction | Focused Spec Driven Development document (SDD), [terminal specification](terminal/spec_driven_development.md) |
| Components, interfaces, data and trust boundaries | High Level Document (HLD), [high_level_document.md](high_level_document.md) |
| Delivery phases, dependencies and rollout | Implementation Plan (IP), [implementation_plan.md](implementation_plan.md) |
| Methods, environments, acceptance and exact cases | Test Plan (TP), [test_plan.md](test_plan.md), and `tests/acceptance/cases.json` |
| Current observed results and blockers | Test Status Report (TSR), [test_status_report.md](test_status_report.md) |
| Requirement IDs, trusted configuration and work graph | `config/requirements.json`, `config/execution-policy.json`, `config/work-items.json` |
| Preserved legacy contracts and source audit | [Contract lessons](migration/current-contracts.md) and [coordination audit](audits/coordination-policy-audit.md) |

`migrate.md` is the required future How-to Guide (HTG), published with tested script-backed commands once the migration implementation exists. Do not invent commands or activate a second live writer while it is pending. The documents remain the behavioral and planning references; machine inputs and the acceptance inventory have their declared owners outside docs. Each detailed rule has one authority and other documents link it.

## Limits and maintenance

The approved product and executable tool govern conflicting legacy instructions. Preserve protected commands, merge flow and useful safeguards. Update behavior explicitly before changing implementation or checks; keep historical source and result evidence. Normal-session activation, public release and production actions retain their separate authorization boundaries.

For a fresh build session, give this instruction:

> Develop and verify the product described here. Read the Repository readme (README), Policy (POL), Spec Driven Development document (SDD), Implementation Plan (IP), trusted execution policy, requirement inventory, work graph and Test Plan (TP), then perform P00 preflight checks before continuing at the first unmet phase. Use the supplied public repository and preserve existing work. Use the recorded authorization with no overall build ceilings while preserving the original start, accounting and all attempts. Keep measured concurrency, reviewed finite batches, per-invocation deadlines and charge reservations; a restart does not reset history. Implement and test in isolated environments, using run-owned TST fixtures, tracked workers with separate worktrees/databases, and both actual installed hosts. Preserve merge and command intent. Rehearse one complete cutover without dual task-state writers. Do not activate normal sessions or deploy production. Freeze criteria, retain failures and distinguish blocked/unrun coverage. Keep durable checkpoints, continue independent authorized work through recoverable failures, stop dependent admission when a current guard or required authority fails and deliver exact artifacts/evidence/recovery state with a truthful readiness decision. Missing required evidence prevents release.

The planned final tests and evidence-supported routine continuations remain authorized in finite reviewed batches. New scope, accounts, paid infrastructure, normal-session activation and production actions retain their stated authorization boundaries; routine implementation choices do not require repeated confirmation.
