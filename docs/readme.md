# AI task manager documentation

This directory is the maintained documentation for the standalone task coordinator being built for Claude Code and Codex CLI. Use these sources to continue implementation and judge readiness; earlier handoff downloads are historical. Product acceptance remains incomplete: current component results and blockers are in the Test Status Report (TSR), [test_status_report.md](test_status_report.md).

## Start

Current delivery authority: the adopted [delivery roadmap](plans/incremental-delivery-proposal.md), [learning protocol](plans/delivery-learning-loop.md) and approved [DP01 plan](plans/deliveries/DP01.md), version 2. Deliver DP01 only; retain the original phase acceptance obligations.

Work from the repository root with Git and Python 3 available. Paths in code spans are relative to that root. Read the Policy (POL), [policy.md](policy.md), then the Spec Driven Development document (SDD), [spec_driven_development.md](spec_driven_development.md), and the Implementation Plan (IP), [implementation_plan.md](implementation_plan.md). Inspect `config/execution-policy.json`, requirements, work graph and acceptance inventory before acting. Documents describe approved work; they do not authorize production actions. The repository address stays in `config/execution-policy.json` so personal identifiers do not need to appear in Markdown.

The user lifted the earlier total spending, elapsed-time and retry ceilings for this approved build. Preserve the original run and accounting, and use finite measured concurrency, reviewed batches and bounded invocations. Use the supplied public repository and the explicitly selected Claude account in `config/execution-policy.json`. Verify its identity before new effects and preserve earlier account-bound evidence. Use the current approved Technical Writer and Skill Builder, and audit affected work when the user supplies an update. Every task prose write needs the appropriate reference and Writing Style (WS) review. A missing second Linear identity blocks live two-user acceptance only; continue independent work.

For this authorized build, the existing primary Linear key may be read from the application repository's `.env.local` as `LINEAR_API_KEY`, without copying or displaying it. Authentication or development-task writes do not establish product fixture acceptance. Verify actual scope before each live fixture run. Docker Desktop must be running for the declared disposable database and Linux-container cases. Standalone verification uses independently authored project fixtures; it does not require the original application stack.

When a distinct second test identity is available, run this in an interactive terminal from the repository root:

```sh
python3 tools/setup_test_credentials.py --secondary-only
```

The helper uses hidden input and stores `LINEAR_TEST_USER_B_TOKEN` outside Git at `$HOME/.config/ai-task-manager/test-credentials.env`, mode 600. It must identify a different user with TST access. Do not paste tokens into chat or test logs.

## Installed inbound listener

The installed CLI supports `aitm --config /private/aitm/service.json --json sync listen` after trusted session setup has completed for that configuration. Its private service configuration names an absolute `receiver_file`; that private JSON file contains `version: 1`, the exact `webhook_id`, a `/hooks/` route with a 16–128 character identifier, `port` (zero selects an ephemeral port), an absolute `secret_file`, and finite `lifetime_seconds` up to 86,400. The secret file contains the selected webhook signing secret. Keep these files owned and mode 0600; the parent configuration directory must be private. The installed command accepts no receiver request payload and rejects a session override that differs from the sealed configuration.

The listener binds loopback and emits a `receiver_ready` record with its address, instance identity and health route. An owned HTTPS tunnel is a separate prerequisite for provider delivery. The listener durably accepts authenticated events, wakes the existing inbound drain and reports `receiver_stopped` with cleanup evidence on exit. Expired or revoked delegation, changed configuration or ownership loss stops admission. Inspect the actual stop record; process exit alone does not prove that provider synchronization succeeded.

## Installed lead and reviewer components

Source `7e4e4f4` includes explicit [initial workflow preparation](workflow_preparation/how_to_guide.md), finite capacity configuration, and the independently reviewed [lead reporting command](lead_reporting/how_to_guide.md) and [independent reviewer configuration](independent_review/how_to_guide.md). Both require sealed installer inputs, distinct role authority, current source/account evidence and finite invocation limits. Before first owner activation, the trusted `workflow prepare` command permits approved intake and enqueue. Ordinary calls still require the owner. Lead reporting is a separate explicit command; its sealed installation chooses human-console or explicitly delegated enrollment. The declared scripted owner cycle has passed, while actual LP17 and required host/model profiles remain pending. These components do not implement automatic idle wake, continuous reporting or the complete coordination skill.

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
| Stable task IDs, parent stories and criterion/evidence links for all plans | [Work-item identity and traceability](implementation_plan.md#work-item-identity-and-traceability) |
| Confirmed Linear records for delivery plans, stories and tasks | [Linear tracking map](plans/linear-tracking.md) and [tracking rules](implementation_plan.md#linear-delivery-tracking) |
| Methods, environments, acceptance and exact cases | Test Plan (TP), [test_plan.md](test_plan.md), and `tests/acceptance/cases.json` |
| Prepare initial approved work before owner activation | [Workflow preparation How-to Guide (HTG)](workflow_preparation/how_to_guide.md) |
| Report a scoped lead event | [Lead reporting How-to Guide (HTG)](lead_reporting/how_to_guide.md) |
| Configure the independent review producer | [Independent review How-to Guide (HTG)](independent_review/how_to_guide.md) |
| Current observed results and blockers | Test Status Report (TSR), [test_status_report.md](test_status_report.md) |
| Requirement IDs, trusted configuration and work graph | `config/requirements.json`, `config/execution-policy.json`, `config/work-items.json` |
| Historical contract lessons and source audit | [Contract lessons](migration/current-contracts.md) and [coordination audit](audits/coordination-policy-audit.md) |

`migrate.md` is the required future How-to Guide (HTG), published with tested product commands once the migration implementation exists. After product completion, a separately requested audit of the then-current target determines which files or sections to remove, replace, edit or retain. Product-owned integration guidance is added only where needed; no old task-management filename is a required survivor. Do not invent commands or activate a second live writer while it is pending. The documents remain the behavioral and planning references; machine inputs and the acceptance inventory have their declared owners outside docs. Each detailed rule has one authority and other documents link it.

## Limits and maintenance

The product must install and work in an empty project. It owns its merge interface and workflow; the retired shell entry point, wrappers, aliases and old instruction paths are not compatibility requirements. AI Index supplies read-only scenarios and lessons to assess critically. No AI Index script copy may live in this repository, including private caches, worktrees, examples or fixtures. Keep necessary immutable source evidence outside the product with hashes and relocation records, without an import or symlink bridge. Preserve useful intended safeguards and every historical result; update approved behavior and checks explicitly. Normal-session activation, public release and production actions retain their separate authorization boundaries.

Expired zero-effect workflow recovery has a [narrow trusted contract](spec_driven_development.md#expired-workflows-without-effects). It preserves the old run and admits at most one linked successor with fresh authority. Public run/resume requests cannot renew deadlines or select recovery authority. Missing original workflow evidence and legacy enrollment without signed provider identity remain separate reconciliation blockers; changing configuration or creating an empty journal does not resolve them.

For a fresh build session, give this instruction:

> Develop and verify the product described here. Read the Repository readme (README), Policy (POL), Spec Driven Development document (SDD), Implementation Plan (IP), trusted execution policy, requirement inventory, work graph and Test Plan (TP), then perform P00 preflight checks before continuing at the first unmet phase. Use the supplied public repository and preserve existing work. Use the recorded authorization with no overall build ceilings while preserving the original start, accounting and all attempts. Keep measured concurrency, reviewed finite batches, per-invocation deadlines and charge reservations; a restart does not reset history. Implement and test in isolated environments, using run-owned TST fixtures, tracked workers with separate worktrees/databases, and both actual installed hosts. Build the standalone merge authority against trusted configured targets and independently authored scenarios. Do not copy or invoke original application scripts or preserve their retired entry points. Rehearse one complete cutover without dual task-state writers. Do not activate normal sessions or deploy production. Freeze criteria, retain failures and distinguish blocked/unrun coverage. Keep durable checkpoints, continue independent authorized work through recoverable failures, stop dependent admission when a current guard or required authority fails and deliver exact artifacts/evidence/recovery state with a truthful readiness decision. Missing required evidence prevents release.

The planned final tests and evidence-supported routine continuations remain authorized in finite reviewed batches. New scope, accounts, paid infrastructure, normal-session activation and production actions retain their stated authorization boundaries; routine implementation choices do not require repeated confirmation.
