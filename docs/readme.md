# AI task manager documentation

This directory is the maintained documentation for the standalone task coordinator being built for Claude Code and Codex CLI. Use these sources to continue implementation and judge readiness; earlier handoff downloads are historical. Product acceptance remains incomplete: current component results and blockers are in the Test Status Report (TSR), [test_status_report.md](test_status_report.md).

## Start

Current delivery authority: DP01, DP02 and [DP03](plans/deliveries/DP03.md) are accepted. The [DP03 delivery handoff](plans/deliveries/DP03-handoff.md) records the verified local code cycle and DP04 prerequisites under the [delivery roadmap](plans/incremental-delivery-proposal.md). DP04 implementation has not started. Preserve accepted evidence, protected resources and original phase acceptance obligations.

Work from the repository root with Git and Python 3 available. Paths in code spans are relative to that root. Read the Policy (POL), [policy.md](policy.md), then the Spec Driven Development document (SDD), [spec_driven_development.md](spec_driven_development.md), and the Implementation Plan (IP), [implementation_plan.md](implementation_plan.md). Inspect `config/execution-policy.json`, requirements, work graph and acceptance inventory before acting. Documents describe approved work; they do not authorize production actions. The repository address stays in `config/execution-policy.json` so personal identifiers do not need to appear in Markdown.

The user lifted the earlier total spending, elapsed-time and retry ceilings for this approved build. Preserve the original run and accounting, and use finite measured concurrency, reviewed batches and bounded invocations. Use the supplied public repository and the explicitly selected Claude account in `config/execution-policy.json`. Verify its identity before new effects and preserve earlier account-bound evidence. Use the current approved Technical Writer and Skill Builder, and audit affected work when the user supplies an update. Every task prose write needs the appropriate reference and Writing Style (WS) review. A missing second Linear identity blocks live two-user acceptance only; continue independent work.

For this authorized build, the existing primary Linear key may be read from the application repository's `.env.local` as `LINEAR_API_KEY`, without copying or displaying it. Authentication or development-task writes do not establish product fixture acceptance. Verify actual scope before each live fixture run. Docker Desktop must be running for the declared disposable database and Linux-container cases. Standalone verification uses independently authored project fixtures; it does not require the original application stack.

When a distinct second test identity is available, run this in an interactive terminal from the repository root:

```sh
python3 tools/setup_test_credentials.py --secondary-only
```

The helper uses hidden input and stores `LINEAR_TEST_USER_B_TOKEN` outside Git at `$HOME/.config/ai-task-manager/test-credentials.env`, mode 600. It must identify a different user with TST access. Do not paste tokens into chat or test logs.

## Installed inbound listener

Use this procedure for a fresh scoped synchronization installation. It supports the public trusted Python setup API followed by ordinary installed CLI commands. The separate `session setup` CLI port supports the [initial workflow manifest](workflow_preparation/how_to_guide.md); it does not currently accept receiver settings. Do not add `receiver_file` to an already enrolled configuration: the changed intent is refused. Do not reactivate archived fixtures or old grants.

### Prepare and enroll the sealed configuration

The trusted installer prepares an owned physical private state directory and service JSON before enrollment. Keep configuration, receiver, secret, credential and capability files owned and mode 0600, with private parent directories. Use the exact installed source, locked dependencies, approved writer package and current verified human/workspace/TST/project/application connection. Uppercase names below are installer-supplied paths, identities, pinned values or the original finite deadline; paths must be absolute.

The service JSON is the existing application configuration, not the operator CLI manifest. Its required fields are `version: 1`, `core_database`, `authority_database`, `authority_key`, `session_file`, `approval_policy` and `linear`. Use these additional bindings:

| Field | Required procedure |
| --- | --- |
| `approval_policy` | Exact approved `writer_hash`, `reference_hash` and `style_hash`; collecting hashes is not approval of a new source. |
| `linear` | Complete expected viewer/workspace/team/project, selected `agent_connection`, one supported credential reference and private provider journal. See [installed Linear credentials](high_level_document.md#installed-linear-credentials) and [connected Linear agent](high_level_document.md#connected-linear-agent). |
| `writer` | Include the existing `root`, `python`, `pins`, `sessions_root`, `journal` and `key` object when preparing new or revised task/progress prose. Keep the approved Python invocation path. |
| `capacity_policies` | Declare the finite host/project limits needed by this installation, as described in the initial workflow guide. Synchronization alone starts no worker. |
| `receiver_file` | Absolute path to the receiver JSON below, present before enrollment. |
| `workflow_control` | May be omitted for synchronization-only task/event access without a native workflow owner. If present, ordinary task/event/`sync now` commands retain that configured owner's forwarding requirement and refuse an absent owner; they never fall back. `sync listen` is a separate explicit operator lifetime. |

Prepare the receiver JSON with exactly these fields; `reconciliation_seconds` is optional:

```json
{
  "version": 1,
  "webhook_id": "CANONICAL_OWNED_WEBHOOK_UUID",
  "route": "/hooks/OWNED_ROUTE_IDENTIFIER",
  "port": 0,
  "secret_file": "ABSOLUTE_PRIVATE_SECRET_JSON",
  "lifetime_seconds": 600,
  "reconciliation_seconds": 300
}
```

`route` uses `/hooks/` followed by 16–128 ASCII letters, digits, underscores or hyphens. `port` is an integer from 0 to 65535; zero chooses an ephemeral loopback port. `lifetime_seconds` is a positive finite number through 86,400. The example's 600 seconds is an example lifetime, not an extension of enrollment. `reconciliation_seconds` must be an integer from 120 to 900; omission uses 300. The secret file must be JSON with exactly `{"secret":"ACTUAL_WEBHOOK_SIGNING_SECRET"}`: 16–4096 visible ASCII characters. Do not put a credential, arbitrary secret or guessed hook identity in place of the actual owned webhook binding.

From the approved installed Python environment, the trusted installer calls the existing API with no factory overrides:

```python
from ai_task_manager.installation.sessions import setup

established = setup(
    CONFIG,
    logical_session=LOGICAL_SESSION,
    actions=("enqueue",),
    deadline=ORIGINAL_DEADLINE,
    operation_key=ENROLLMENT_KEY,
)
assert established["capability_saved"] is True
```

Verify the returned `scope` against the intended identity/project/session and preserve `enrollment_ref`. Include `existing_issue_selection` in the explicit actions only when that installation must select existing issues. Setup signs the unchanged service intent, including the receiver path, and saves its capability. The listener captures and continuously checks the receiver/secret files at startup; this API does not generate their contents or extend any original authority. Prepare them before enrollment and retain the same files through the receiver lifetime. Literal `session doctor` diagnoses installations created by the operator CLI port; it is not the readiness gate for this API-created configuration.

### Start the installed receiver and read its events

Run the absolute installed executable from the intended project with `PYTHONPATH` unset and its explicitly selected credential source available:

```sh
AITM --config CONFIG --json sync listen
```

The command accepts no receiver request payload or alternate installation. It uses default service composition and the enrolled Core/provider stores; it does not require a native owner when `workflow_control` is omitted and does not create a worker. A second listener for the same Core file refuses.

Require an actual `receiver_ready` line. Use its observed loopback host, port, route, `instance_id` and `health_route`; do not infer them from a PID or old log. The listener owns a finite loopback HTTP endpoint. Genuine Linear delivery also needs the separately owned HTTPS transport and exact TST Issue webhook under their approved setup/cleanup contracts. Match the public route, current receiver instance, webhook identity and signing secret before enabling provider delivery. The listener itself neither creates a tunnel nor provisions a webhook.

Authorized local task changes use the shared application API, save durable outbound intent and trigger confirmation immediately. Compare returned synchronization state with independent provider readback. The installed `sync now` command drains existing saved outbound/inbound work once; it is not a command to force a registered-issue polling scan or discover arbitrary backlog.

Read the same session's task and event projections through ordinary installed commands. For example, place `{"after":0,"timeout_seconds":0}` in `EVENT_REQUEST` and run:

```sh
AITM --config CONFIG --json task list
AITM --config CONFIG --json event list --input EVENT_REQUEST
```

Use a returned sequence as the next `after` value for this session. Event listing and a successful provider write do not advance host delivery, AI acknowledgement or user-report receipts. Those stages need their separate actual observations through the [lead reporting procedure](lead_reporting/how_to_guide.md). This procedure makes no claim that an idle lead woke up.

### Confirm synchronization and recover a missed change

A normal authenticated webhook is durably accepted before HTTP 200 and wakes the inbound drain immediately. HTTP 200 alone does not prove application: legitimate ignored or duplicate deliveries may also return it. Verify actual Core/CLI/event state and provider readback, with separate event, receipt, application and render timestamps. Invalid authentication, stale transport time, changed business content under a reused delivery ID and lost current authority remain refusals. A refreshed valid transport timestamp does not create a second business effect.

The listener performs registered-task recovery on startup, then schedules recovery polling. The configured interval defaults to 300 seconds, with at most eight registered tasks per slice and a round-robin cursor; larger populations need later eligible slices. Webhooks remain the normal path and can run while a recovery read is outstanding. Rate/transport failures use the existing bounded backoff and provider `Retry-After`; wakes do not bypass pacing. Report actual delay rather than promising an internet-wide latency bound.

A poll can detect and stage a missed provider change while preserving a hold, conflict or unknown actor. It cannot invent a signed delivery, human instruction, queue admission, worker or acknowledgement. `sync now` cannot replace that check. For a controlled missed-change test, observe the next eligible scheduled read or a supported same-configuration receiver restart under its unchanged still-valid enrollment; compare exact remote/local facts and retained operations. Keep webhook authentication and current authority checks active.

### Stop or handle a refusal

Stop only the confirmed owned receiver through its normal signal/lifetime path, then inspect the actual `receiver_stopped` record and confirmed process exit. Require `cleanup.stopped`, listener/drain/reconciliation stop and ownership release; process disappearance alone is insufficient. Expiry, revocation, changed files or uncertain ownership can produce a failed command with retained durable facts. Preserve that original outcome. Receiver shutdown does not retire the separately owned tunnel, webhook, project or issues; use their supported scoped cleanup and original identities.

If startup refuses, retain its stage diagnostics and original configuration/operation. Correct only the named prerequisite through the supported installer; do not edit grants, replace stores, remove an unknown owner's lock or silently extend a deadline. These are supported synchronization procedures, not complete release, production or normal-session activation approval.

## Human instructions and held-work settlement

A signed Linear event can record an attributable human change immediately, fence old writes and hold work. It does not prove that an active worker stopped. The owning runtime must retain capacity/resources until its existing `ManagedWorkers.stop` or `reconcile` path records the actual complete stop through runtime evidence and Core settlement. Unknown process ownership remains unresolved. Task and attempt views report `stopping_or_unresolved` while old work is fenced; a remote In Progress or delivered status cannot supply startup or delivery evidence.

For an explicit operator instruction, the trusted installer uses the existing independent console and an already owned `HostJournal`. This is a Python operator API, not a model tool or a JSON approval importer. Use current task/version/decision-revision values, the original finite provider grant and the actual existing native journal/key. An absent journal or console is a prerequisite failure; do not create a replacement store, use a retired journal or write observer rows by hand.

```python
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.installation.sessions.human import capture_instruction

human_ref = capture_instruction(
    CONFIG,
    HostJournal.open_existing(HOST_JOURNAL, HOST_KEY),
    task_id=TASK_ID,
    target="Todo",
    expected_version=TASK_VERSION,
    decision_revision=DECISION_REVISION,
    reason="Release this exact held task after reviewing its current scope.",
    timeout=60,
)
```

Uppercase values are explicit installer inputs; paths are absolute. Inspect the actual current work and, for a prose revision, the approved Writer output before approving the console's exact one-use challenge. Supply `writing_ref=APPROVED_WRITING_REF` when the instruction authorizes that proposed revision. The capture binds the exact task, revision, current/proposed prose digests, original config/capability, host journal/key and current provider enrollment. Cold consumers verify that source again. Synthetic terminal input establishes an authored operator test only, not a person's acceptance.

Pass the returned `human_ref` to the existing task action, revision or worker-start operation with current concurrency fields. `review_decision` also requires `review_target`; `worker start` retains actual startup requirements. Every configured In Review destination keeps its cause-specific prerequisites. Merge, staging, production and non-code completion references must come from their existing trusted producers; human approval alone supplies none of them. Staging/production producers remain isolated contracts here, not actual deployments. A new instruction must enter within the capture's admission window. A recorded current decision can continue through its `decision_ref` when missing evidence later arrives, while the original provider grant, source files, work digest and decision revision still hold.

To settle an existing pending signed observation after stop or approved-prose evidence arrives, inspect `task external_changes` and the current task, then place this request in a private input file:

```json
{
  "task_id": "CURRENT_TASK_UUID",
  "observation_id": "CURRENT_PENDING_OBSERVATION_UUID",
  "expected_version": 12,
  "decision_revision": 3,
  "cause": "hold",
  "evidence_refs": {},
  "operation_key": "ORIGINAL_SETTLEMENT_KEY"
}
```

The numbers are examples, not authority. Run `AITM --config CONFIG --json task settle_external --input REQUEST`; MCP exposes the same `task_settle_external` operation. It verifies current provider/connection authority before and after fresh readback and compares exact issue/revision/status/prose/assignment facts with the original observation. Core checks current versions, actor, cause, quiescence and destination evidence. Revised external prose first needs the existing `task accept_external_prose` path and exact approved readback. Unknown actors, newer facts, foreign assignment, revoked delegation, missing stop/delivery evidence and illegal reopen/delivery remain held or conflicted.

This operation creates no outbound write and does not rewrite the original delivery's conflict/error history. An exact committed retry reports current local facts without repeating an effect; changed arguments require their own supported operation. Ordinary calls still pass through the configured owner when `workflow_control` is present. Neither operator capture nor settlement replaces that owner.

Connection selection remains explicit and sealed per logical session. The adapter resolves the canonical application ID in the current workspace, preserves the separate human assignee and refuses missing/ambiguous/non-application, permission/account or competing-session selections. A different delegate needs a new explicitly configured logical session and its own authority; do not edit an enrolled selection in place or reuse an old connection for an uncertain send.

## Installed lead and reviewer components

Source `7e4e4f4` includes explicit [initial workflow preparation](workflow_preparation/how_to_guide.md), finite capacity configuration, and the independently reviewed [lead reporting command](lead_reporting/how_to_guide.md) and [independent reviewer configuration](independent_review/how_to_guide.md). Both require sealed installer inputs, distinct role authority, current source/account evidence and finite invocation limits. Before first owner activation, the trusted `workflow prepare` command permits approved intake and enqueue. Ordinary calls still require the owner. Lead reporting is a separate explicit command; its sealed installation chooses human-console or explicitly delegated enrollment. The declared scripted owner cycle and the scoped genuine DP02 non-code cycle have passed. Historical LP17 reporting failure and the remaining required host/model profiles stay visible in the Test Status Report (TSR). These components do not implement automatic idle wake, continuous reporting or the complete coordination skill.

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
| DP01 result, supported procedure and DP02 handoff | [DP01 delivery handoff](plans/deliveries/DP01-handoff.md) |
| DP02 approved scope and continuation | [DP02 plan](plans/deliveries/DP02.md), [session prompt](plans/dp02-session-prompt.md) and [continuation handoff](plans/deliveries/DP02-handoff.md) |
| DP03 approved scope and execution | [DP03 plan](plans/deliveries/DP03.md) and [execution prompt](plans/dp03-session-prompt.md) |
| Approved DP04 scope, bounded audit, stories, tasks and acceptance | [DP04 detailed plan](plans/deliveries/DP04.md); version 1 is approved for complete scoped autonomous execution |
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
