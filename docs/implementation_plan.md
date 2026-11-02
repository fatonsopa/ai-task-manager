# AI task manager implementation plan

Status: Approved Implementation Plan (IP); implementation and acceptance are tracked separately in the Test Status Report (TSR), [test_status_report.md](test_status_report.md).
Sources: [Policy (POL)](policy.md), [Spec Driven Development document (SDD)](spec_driven_development.md), [High Level Document (HLD)](high_level_document.md), and `config/execution-policy.json`.

## Goal and specification

Build an installable task coordinator for Claude Code and Codex CLI. Deliver the approved behavior, self-contained packages and evidence that every required acceptance cell passes. A written implementation or a passing component suite alone does not satisfy the goal.

The Policy (POL) owns authority and roles; the Spec Driven Development document (SDD) owns lifecycle rules, protected command/merge behavior and failure/receipt paths. The High Level Document (HLD) owns architecture. The Test Plan (TP), [test_plan.md](test_plan.md), owns assessment methods, environments and exit criteria. This plan owns delivery order and recovery preparation. R01–R38 and case IDs remain in the existing registries.

The user lifted the build's total spending, elapsed-time and retry ceilings. `config/execution-policy.json` records this authorization, finite execution controls, publication boundaries, MIT holder and required host/model profiles. Preserve the original run, accounting and every attempt through restarts; normal-session activation, public release and production changes retain their separate review boundaries.

## Current context and approach

The latest source input is `873155ac73f486912e4e9fe6e3daa719388e0665` on `origin/dev-mirror`, five commits beyond the previously inspected `24d05ac23420b0bb2b062430ff4e9b9c78727b81`. The merge bytes remain the recorded baseline; changed worktree/test-stack helpers, fixture shapes and monitor waiver hooks need a separate compatibility/migration audit. The local source checkout remains `15992e2be8b2fe47d4646f92e62c90888a332619`; earlier pins and the `c05f40200703e7b8f379a8477502dabd594ba79c` inventory remain historical evidence. `config/source-baseline.json` owns exact pins/hashes. Recheck source changes before compatibility execution; no new audit or application probe is claimed here.

The pinned application snapshot has `scripts/linear/status_policy.py`, `linear_sync.py`, `issue_cli.py`, `subagent_guard.py`, `agent_capacity.py`, worktree/database scripts, task hooks and task-management instructions. Its legacy end-to-end harness lives under `scripts/linear/test_tasks/`. It creates disposable repository copies and real Linear projects in TST. Audit and reuse its verified mechanisms instead of copying its full policy implementation into the new core.

The harness declares untested paths in `fixtures.NOT_DRIVEN`, including connector status writes and a killed-session observation. These are gaps to investigate, not passes. Its `close_out.py --start` operates across open TST issues; do not use that team-wide cleanup in a concurrent build. Use run-owned fixture projects and validate ownership before `--project` cleanup. Capture case verdicts before cleanup changes fixture statuses.

Use the architecture's component/data/packaging design instead of importing the monolithic synchronizer. Test existing project procedures only in complete disposable copies. Skill Builder produces independently installable skill repositories outside this checkout, then verified pinned artifacts are assembled into the product. Do not alter either source skill to fit this product.

### Existing-issue intake

TST-85 adds the focused `task_find`, `task_inspect` and `task_admit` operations. The frozen contract and 84 case families are retained under `tests/existing/frozen/`. They extend the existing acceptance work without replacing R01–R38 or the global case inventory.

The provider reader verifies metadata scope before retrieving prose, then checks the complete state timeline and a fresh revision. The core binds reviewed exact content and a separately observed lead selection to a transactional canonical claim. Admission, queue and each startup phase obtain a separate complete reread and a 30-second receipt bound to the consuming operation, task/version, decision revision and enrolled store. Core verifies that context at the transaction boundary; it cannot substitute a newer version for the caller's expected version. A committed same-key retry preserves its earlier result without another effect.

A new remote Todo binding remains unqueued until the existing enqueue authorization confirms session membership. Prose confirmation or remote settlement cannot establish imported queue membership. Queue/start checks and permanent delivery/terminal fences share core rules; positive delivery also guards new prose authority and linked-parent eligibility. Lookup does not rewrite cached Task flags. No remote issue is created as a lookup fallback.

The additive schema-3 migration preserves task/outbox/resource history and introduces stable store identity and lifecycle records. The installer must explicitly call the trusted claim-store enrollment port before product composition; ordinary runtime configuration cannot enroll a replacement. A copied or replaced store is refused even at the old pathname. Same-file renames can retain enrollment after an explicit configuration update. Restore/replacement integration still needs verified quiescence and retained ownership; this task supplies no automatic restore command.

Integrate the implemented selection-receipt contract with the actual host observer before claiming the live selection path. Keep the separate delivered-follow-up issuer, complete installation/cutover, application resource/merge adapter and live end-to-end receipt work visible as dependencies. Installed CLI/MCP tests with authored loopback transport establish their mechanical boundary, not those live paths. Exact observations and external-conflict fencing bound the remaining cross-system race; SQLite cannot lock Linear against a later edit.

The enrolled-store guard validates a bounded identity write on the same SQLite connection inside a rolled-back savepoint before authority reads and before commit. Require DELETE rollback-journal mode and a writable store; refuse other modes without converting them. Keep the original identity and outer domain writes intact. This adds small journal I/O and is not descriptor attestation, hostile same-user protection or support for live file replacement. Verify the exact copied-connection/restored-path race, crashes and human-hold ordering on each actual supported runtime.

### Tools and prerequisite checks

| Tool | Purpose and required check |
| --- | --- |
| Git and GitHub CLI | Clone the supplied repository, create isolated branches/worktrees, inspect diffs and push authorized code. Detect existing content/default branch before initialization; preserve unrelated files. |
| Python, `uv`, SQLite | Core implementation, locked environments, metadata migrations and test execution. Verify supported interpreter/dependency combinations; the observed Python version alone is not a compatibility commitment. |
| `pytest`, Hypothesis, coverage tooling | Domain/contract/process tests and state-machine property testing. Review tests and fault seams before execution; inspect uncovered behavior rather than imposing a meaningless line-coverage percentage. |
| Ruff and a selected type checker | Static checks and import-boundary checks. Freeze tool versions and use one project configuration. |
| Official MCP SDK | Typed shared tool interface. Validate request/response schemas, session capability and error semantics; do not duplicate operations in shell wrappers. |
| Claude Code CLI | Actual host integration and behavior runs with structured traces and a declared tool policy. Use installed `--help` and current official documentation. Avoid modes that disable required hooks or configuration discovery. |
| Codex CLI | Actual host integration and JSONL traces. The inspected version supports `codex exec --json --sandbox workspace-write`; do not copy retired `--full-auto` examples. Resolve authentication, approval and hook trust before the unattended run. |
| Docker Desktop | Disposable application databases, fixture stacks and real Linux containers on this Mac. Require a running daemon and prove a small container works before dependent application-database/Linux cases. |
| Textual and `tmux` | Interactive companion pane and terminal session layout. Verify the installed dependency and actual mouse/keyboard behavior; do not claim a native host sidebar API. |
| Pseudo-terminal harness | Drive terminal focus, clicks/keys, resizing and output assertions. Use real PTY evidence alongside application-level UI tests. |
| Existing project scripts | Worktree/database preparation, current merge and pipeline compatibility inside disposable copies. Inspect before invoking; inherited documents do not authorize production actions. |
| Technical Writer and Skill Builder | Document quality and skill production/evaluation. Use their installed environments, typed runners and actual version hashes; do not guess dependencies or claim fresh behavior from replay. |

The table describes checks to perform, not current availability. `config/research-sources.json` retains preparation-time versions and observations; consult the Test Status Report (TSR) for later evidence. Inspect installed help/current official contracts before executing version-sensitive commands. Authentication alone does not prove model execution, and a local fixture does not prove live acceptance.

Use the approved `claude_roni` profile: resolve its configuration under `$HOME` and pass `CLAUDE_CONFIG_DIR` explicitly to the unmodified `claude` executable. The original `claude` account is unavailable; refuse fallback. The Test Plan (TP) owns credential handling, fixture scope and required distinct-user checks.

## Work sequence

The machine-readable dependency graph is `config/work-items.json`. It contains 11 phase gates and 34 planned work items, including T34, the repository responsibility/layout audit and packaging prerequisite. Split a work item further when necessary, preserving its acceptance and dependencies. Every implementation task gets its own tracked assignment and worktree; overlapping owned files require coordination or serialization. A phase is complete only when its output and evidence pass review.

### P00 — Bootstrap and verify prerequisites

Read the policy, source baseline and current source instructions. Resolve `SOURCE_REPO` from the actual checkout; `$HOME/projects/claud-version-ai-index-2.0` is a discovery candidate, not a machine-specific fixed path. Resolve `PRODUCT_REPO` in a separate build workspace. Clone the remote from the policy using a structured subprocess argument list. GitHub reported public visibility and no default branch at inspection; verify current refs before choosing the first branch. Never overwrite existing work.

Start a durable build-run ledger outside the product repository. Record run ID, source and requirement hashes, UTC start, effective policy revision and any applicable deadline, host/model/effort profiles, actual tool capabilities, allowed paths, spend ledger and finite resource limits. The initial two-worker limit can rise after measured evidence. The build controller itself does not consume a worker assignment for bookkeeping.

The bootstrap may use the existing host's task/delegation tools and a deterministic build ledger until the new engine passes its gates. This is development scaffolding, not a claim that the new product is already enforcing assignments. Register development work in the run's TST project through an audited isolated path. Workers still receive one task and separate worktrees and fixture databases. Keep bootstrap scheduling separate from product policy; replace scaffolding only after its replacement is tested.

Prove authentication, file access, model access, network, Docker, disk/memory headroom, worktree creation and a tiny disposable database operation. Run dependency probes in the same environment and permissions as the eventual cases. Resolve host approvals and hook trust once in the intended configuration; do not suppress host safeguards to make tests pass. Missing capabilities block only dependent work and remain explicit in the final result.

P00 uses the claude_roni profile for Claude authentication and authorized model-access probes. Resolve its configuration path under the user home and pass CLAUDE_CONFIG_DIR explicitly to the unmodified claude executable in automated processes; shell functions need not exist there. The original default account is disabled. Both-host acceptance still requires actual tool-enabled evidence.

Gate: trustworthy environment inventory, nonempty authorization/limit record, working policy, cost and capacity admission with per-invocation deadlines and a durable checkpoint. Verify the primary test identity before live TST writes. A missing secondary Linear identity blocks two-user cases only; missing Claude access blocks Claude execution tests only. Keep independent core/mechanical and available Codex work eligible. Do not start an unmetered paid batch.

### P01 — Freeze contracts and baseline

Inventory the current `task-management.md`, lead instructions, Linear policy rows, task/subagent guards, resource scripts, task storage and every task-state writer. Include tool hooks, Git hooks, timers, watchers, background processes, pipeline callers, direct connector writes, user settings and nested instructions. Search actual call sites and inspect dynamic execution/configuration, not just filenames.

Create a migration table with one row per rule/caller: source location/hash; intended behavior; retain/change/retire decision; owning new module; preserved command surface; state to import; disabling/fencing action; tests and evidence. User-approved transition changes are intentional improvements. Record their difference from the baseline instead of silently treating old behavior as the new requirement. No unmapped row is allowed at cutover.

Run separately tracked T34 in its normal dependency order before packaging: audit the whole current tree, actual imports and every consumer using current Skill Builder responsibility/domain guidance. Classify executable helpers, runtime configuration, test/evaluation definitions, generated records and documentation assets using the target ownership table in the High Level Document (HLD). Keep prose, editable/rendered diagrams and screenshots under `docs/`. Freeze exact old/new paths and regressions before moves/refactors; preserve a verified before-image and recovery manifest. Update all consumers, entry points, package/install checks and current links together. Do not move files merely by extension or create network services for folder naming; no broad structural implementation occurs in this documentation task.

Expand the authored acceptance manifest using the real policy's complete pair/cause matrix, workflow branches and legacy contract inventory. Specify actual fixtures, negative controls, fault seams and oracles. Review tests before execution and freeze cases/criteria before tuning. A genuine new requirement creates a versioned objective; do not weaken expected results after a failure.

Run the audited existing harness in an isolated repository copy with run-scoped TST cleanup. Preserve every baseline failure and `NOT_DRIVEN` case. Use no team-wide cleanup and no production deployments. Gate: reviewed contracts, reproducible fixtures, baseline report and complete ownership mapping for implementation.

### P02 — Implement core records and admission

Implement pure domain rules and one application API. Separate task intent, execution attempt, worker identity, resource owner, delivery evidence, human decision and synchronization operation. Keep domain imports independent of providers and presentation. Enforce boundaries with executable import checks.

Implement schema-versioned SQLite storage, optimistic versions, unique claims, atomic capacity reservations, append-only audit events and durable outbound intents. External process/API actions do not participate in the SQLite transaction: reserve, perform, confirm and compensate/reconcile explicitly. A schema migration must preserve a before-image and have a tested recovery path.

Gate: all pure policy, scope, transition-pair, transaction and architecture tests pass on exact artifacts. Invalid or premature actions change no unauthorized state.

Before merge integration, perform the separately tracked latest-source compatibility audit under R08 revision 3. Keep the current route unless a demonstrated incompatibility prevents approved goals; missing new runner support is not sufficient. If necessary refactoring is justified, freeze existing-scenario regressions first, preserve the command contract and verify disposable-copy migration/recovery before cutover. The documentation update itself performs none of that implementation.

### P03 — Implement workers, resources and the first complete path

Prepare owned worktrees and databases with durable identifiers, confined paths and compensation for partial setup. For product-development tasks, use separate SQLite fixture databases; project-adapter cases require the existing application's disposable database stack per worker. The control store is never a worker's application database.

Launch and supervise actual worker processes, recording PID plus process-start identity, host session/thread ID and attempt capability. Distinguish startup, liveness, progress and exit. A heartbeat is not progress; a worker message is not proof of success. The startup handshake must prevent task execution before the task and remote assignment are confirmed.

Prove one real task through registration, one worker, isolated resources, a committed fixture change, the existing merge script and checked cleanup. Reconcile final Git and Linear state. Add refusal and crash cases at each side-effect boundary. Gate: the complete one-worker path and its recovery work before increasing concurrency.

### P04 — Implement Linear policy and live integration

Implement typed intake/queue/hold/resume/accept/cancel/duplicate/delivery operations. Apply the transition table, immutable delivered-issue start guard, human authority and verified non-code completion. Derive workflow-state IDs from the bound team; do not reuse state IDs between TST and the application team.

Use one durable synchronization writer. Record operation identity before remote requests, reconcile lost responses, read back mutations and report pending/failed synchronization separately from execution. A partial API response or missing history is uncertainty, not proof of an absent human decision. No blind retry of uncertain issue creation.

Drive the live TST scenarios, including distinct-user isolation, holds, dependencies, assignments, review, duplicate/cancel, partial issue completion and delivery stages. Use a real merge in the disposable copy; staging/production events are explicitly isolated fixtures. Gate: real readback agrees with local facts; negative scope attempts and concurrent-run cleanup leave sentinels unchanged.

### P05 — Integrate Claude Code and Codex CLI

Expose the same application operations through CLI and MCP. Install the actual package into isolated host sessions and verify skill discovery, MCP availability, hooks, argument schemas, error behavior and current host trust. Do not replace real host execution with a mock response.

Implement supervision around supported CLI structured events first. Use a supported persistent protocol only when required lifecycle control cannot be established through the CLI; record and test that choice. Capture actual final status and tool events, not just exit code or final prose. A shell process starting does not establish a successful model turn.

In strict managed sessions, block native task-state mutation and unmanaged subagent creation through every supported entry point. Test native agent tools, shell launch attempts, SDK/CLI paths, direct MCP calls and stale configurations. Engine capabilities must be checked server-side. General-purpose shell access is not a universal security sandbox: if an installed host cannot enforce the declared managed surfaces, mark that host integration incomplete instead of claiming an absolute anti-bypass guarantee.

Gate: both real CLIs complete a scoped installed smoke workflow and refuse unsupported state/agent actions. Neither host silently falls back to an unguarded mode.

Use claude_roni for Claude host tests and Codex for its own host tests. Verify the profile on every worker launch and refuse fallback to the unavailable default account. Authentication preflight is not model-performance evidence. Every selected host/model/effort profile requires its own actual traces and complete required coverage.

### P06 — Complete scheduling and recovery

Implement dependency-cycle rejection, exact blocker reasons, explicit dependency fulfillment, approved queue ordering and starvation visibility. Do not equate failed/canceled predecessors with dependency success. Honor project-specific file/migration/test-lock conflicts through adapters, without scheduling arbitrary team issues to keep workers busy.

Implement and exercise recovery through the fault matrix in the [Test Plan (TP)](test_plan.md#failure-and-recovery). Inspect actual processes, Git, databases and remote facts before restarting; unknown state prevents competing work.

Gate: the state-machine, process, fault and capacity tests preserve invariants with deterministic seeds and repeatable reproductions.

### P07 — Implement the terminal views

Build scoped JSON/text CLI output first, then the right-hand companion pane and passive status line. Implement the approved grouping, activity, inspection, controls, scrolling and native-host layout from the [focused Spec Driven Development document (SDD)](terminal/spec_driven_development.md) through the shared scoped projection.

Use the maintained activity symbols and accessibility requirements in the [terminal specification](terminal/spec_driven_development.md#quality-and-interface-constraints). These are display conventions, not another workflow policy.

Gate: real PTY interaction and own-session isolation tests pass. A screenshot alone does not establish click or keyboard behavior. A fixed native host sidebar extension is not assumed.

### P08 — Build skills and run fresh behavior evaluations

Use the current approved Technical Writer and Skill Builder versions, recorded by source hash. The user released the temporary hold to avoid blocking the build. When a new version is supplied, audit affected work, revise what needs changing and rerun impacted checks while retaining prior evidence. Task prose must pass Technical Writer with the appropriate reference and Writing Style (WS) guide before publication.

Invoke the provided Skill Builder through its actual root `SKILL.md`. Use its installed dependency environment and typed start/packet/submit/evaluate/review/apply workflow. Keep its run records outside the skill repositories. Use the supplied MIT holder, focused domains, executable mechanisms and independent installation checks. Use impacted tests during iteration and the final relevant suites for the final candidate; the approved build authorizes complete acceptance through finite reviewed execution batches. Preserve source-skill managed state and all earlier closed packets.

Run the frozen host/profile evaluation matrix, repetitions and evidence controls in the [Test Plan (TP)](test_plan.md#fresh-behavior-evaluation). Gate: its required host/profile cells and relevant final checks pass on the exact candidate; missing observations remain blocking.


### P09 — Implement complete cutover and distribution

Implement all migration inventory rows before enabling the new system in a normal project. Preserve current command surfaces through thin tested compatibility entry points where needed. Convert `task-management.md` to the concise current-system contract in the same activation. Generate transition reference material from policy definitions and test documentation parity. Keep rationale, operator guidance and project exceptions readable; the engine owns enforcement.

Retain the project's `.claude/standards/task-management.md` path for current consumers, replacing its contents at cutover with the new roles, commands, exceptions and maintained references. Remove obsolete procedures only through the completed rule inventory. Keep root agent instructions concise. Claude's root instructions can use `@AGENTS.md` for shared rules; test actual import discovery and use supported Codex instruction mechanisms rather than assuming Claude import syntax works there. Detailed generated matrices and test history are loaded when needed, not automatically added to every session.

Build self-contained Claude and Codex packages from the same core and pinned skill artifacts. Native plugin formats and tool discovery may differ; test the installed artifacts, not just source directories. Implement install/doctor/update/disable/uninstall and state export/recovery. Resolve ownership and compare current settings before restoring any before-image. Do not store authoritative state in plugin caches or remove another integration's files.

Rehearse the clean cutover in a complete application clone with existing tasks, holds, live workers, a merge, pending sync, worktrees, databases and stale sessions. Include every old writer and instruction surface from the inventory. No dual live operation is an acceptable intermediate state. The full cutover and rollback procedure is below.

Deliver `docs/migrate.md` with exact, tested commands to run inside AI Index and an inventory-bound removal/replacement manifest. A migration script performs the work: validate the prepared replacements and backup, fence old writers, remove obsolete files, install replacements, then verify the new authority before enabling it. Preserve protected commands and the versioned merge contract; apply necessary implementation refactoring only through the audited R08 gate. Test interruption between removal and replacement, resumable recovery and rollback without two live writers. The Markdown guides the operator; it is not itself executable code.

Gate: all required records and contracts migrate; old writers are fenced; the new system alone passes installed acceptance; repeated install/remove and interrupted upgrades preserve data.

### P10 — Complete acceptance and prepare delivery

Run the full final suite against the exact candidate artifacts, including live TST, installed host behavior, clean cutover, failure recovery, real terminal interaction, install/remove, second-project portability and Linux-container coverage. Fix failures through bounded, evidence-based iterations, then rerun impacted cases and the required final release checks. Preserve all attempts.

Use remaining time for a measured mixed-workload endurance run with deterministic faults, multiple sessions, dependency chains, varied task sizes and restarts. Record the actual duration, workload, concurrency and invariant checks. Do not claim a 48-hour soak from a shorter test or treat the build's elapsed time as endurance evidence.

Gate: the release checker accepts every required case/profile, source and package hash, migration row and evidence record. Produce the final evidence report, install/remove commands verified against the built version, operator recovery guide, known limits and integration patch. Normal-session activation remains a final review action.

### Autonomous operating procedure

The build controller migration is a separate verified operation: prepare a fresh private backup, bind exact authorization/current-state hashes, refuse unresolved launching intents, and apply the versioned amendment through its supported API. Current declared policy does not itself mutate the original ledger. Keep that migration pending until the candidate and exact command pass lead review.

Preserve the original build run and UTC start. Record the authorized policy amendment and its receipt without resetting elapsed usage, calls, spending or failures. The amended total ceilings are explicitly absent; keep finite measured capacity, reviewed batches and a monotonic timeout for every supervised invocation. A policy change or expired timer cannot establish that a worker stopped, release uncertain capacity or justify killing a merge blindly.

Choose finite invocation and spending allowances for each reviewed execution batch. Include workers, lead invocations, graders, retries and chargeable services in the same durable accounting. A later batch is an evidence-supported continuation, not a fresh run or a way to erase earlier usage. There is no overall retry-round ceiling under the recorded user authorization.

Use one atomic accounting ledger and finite batch reservations. Before a paid call, reserve a defensible maximum charge from current official pricing and enforced provider/transport limits; reconcile actual usage afterward. Parallel calls cannot reserve the same batch allowance or worker capacity. If a CLI transport cannot establish a bounded charge or usage provenance, use a supported bounded transport or block that paid call. Subscription sign-in alone does not establish unlimited allowance, zero incremental cost or a spending cap. Unpriced tools and new paid infrastructure remain unavailable.

Choose phase batches by dependencies, actual capacity and cost. The build runner schedules bounded model turns and deterministic commands, consumes their structured outcomes, persists a checkpoint, and resumes the next eligible task. A single interactive chat turn does not establish a continuously supervised background service. Persist child process identities, model turn IDs and job handles so loss of the chat does not create duplicate launches. If the host cannot sustain supervised background execution, report that preflight limitation before starting an unattended claim.

Use measured progress and phase dependencies to update forecasts and the eligible work queue. Retain earlier milestone estimates as historical planning evidence; they are not current deadlines or permission to bypass a phase gate. Report delays and remaining work without removing accepted requirements.

At each completed work item and at least every five minutes while work runs, checkpoint current phase, issue/task/attempt IDs, worker identities, Git heads, resource ownership, latest evidence, pending sync, charge reservations, original elapsed usage, current invocation/batch bounds and exact next actions. Keep secrets out. A fresh context must inspect this checkpoint and live facts before acting. Send human-facing updates on meaningful progress, failures, decisions and completion; do not emit unchanged heartbeat noise.

For a failed case, preserve the attempt and diagnose its cause. Continue in finite reviewed batches with actual evidence, a checkpoint and a concrete reason; the lifted overall ceilings do not permit an unbounded individual process. A new hypothesis needs source evidence, while a supported unchanged-case retry does not require a code change or another user approval. Never edit answers, graders, journals, frozen criteria, results or source-skill managed counters to simulate a reset. Report a still-incompatible invoked guard for a separately scoped transition; do not bypass it. Failed or missing acceptance remains incomplete.

## Requirement coverage

Implementation ownership remains grouped below; the Test Plan (TP) and versioned `config/acceptance-cases.json` own detailed verification/evidence requirements. The work graph in `config/work-items.json` retains all phase/task IDs and dependencies. Component observations do not silently complete those exit gates.

| Requirements | Implementation area |
| --- | --- |
| R01–R03, R11 | Intake, execution and host adapters |
| R04, R12, R23 | Scope policy and Linear adapter |
| R05–R07, R19, R31 | Resources and scheduling |
| R08–R10, R13, R30, R32 | Policy, project/legacy adapter and sync |
| R14, R20, R26 | Supervisor, recovery and build runner |
| R15–R18, R24, R27 | Application API, host packages and presentation |
| R21–R22, R25, R29 | Evaluation harness and release checker |
| R28, R33–R34 | Migration and maintained instructions |
| R35–R38 | Maintenance, complete lifecycle, real-time events and writing approval |

The acceptance inventory remains 248 authored scenarios across 14 workflows, covering all 38 requirements. These counts describe the specification, not executed acceptance. P01 expands exact source-derived fixtures and transition pair/cause oracles without weakening authored expectations.

## Rollout and recovery

### Clean project-wide cutover

Development and testing happen in isolated copies while the current real system stays unchanged. Activation in the normal project is a separate reviewed action. At that action, there is exactly one cutover; no shadow writer, dual synchronization, gradual live task ownership or legacy sessions continuing to mutate task state.

1. Verify exact release artifacts and the completed inventory. Validate state exports/imports, backups and prepared replacements; preserve task/issue/attempt/resource identities, human decisions, pending operations and history.
2. Stop project-wide admission. Let the currently active merge route finish; checkpoint and observe other workers quiescent. Account for sessions, process trees and delegated work before proceeding; a process merely believed stopped is insufficient. Fence every old hook, watcher, timer, materialized script and stale session under the authority epoch.
3. Import and reconcile all required state against Git, resources and Linear. Quarantine ambiguous records and keep activation stopped; do not invent missing values.
4. Execute the validated inventory-bound removal/replacement transaction: remove only listed obsolete files, install the prepared replacements, then verify callers, scoped configuration and the protected versioned merge contract, including any justified, regression-verified R08 change. Interruption here must resume safely or restore validated owned files while admission remains fenced.
5. Activate one new authority only after replacement validation. The prepared configuration uses an atomic activation marker. New client startup checks the epoch and rejects old task-management mutation paths. Confirm old-session rejection, installed routing and synchronization before admitting normal work. Retain the recovery snapshot and audit record.

These are implementation/delivery steps. The Spec Driven Development document (SDD) owns the observable cutover guarantees; the future `migrate.md` How-to Guide (HTG) owns exact tested operator commands once the migration script exists. No dual writer is an acceptable intermediate state.

### Failure, rollback and uninstall

A failure before activation leaves admission stopped and the old writer set fenced until an explicit recovery path verifies restoration. A failure after new writes requires reconciliation/export of those writes before considering restoration. Never blindly restore an old database snapshot, re-enable old watchers, or run two engines to catch up. A safe stopped system is preferable to contradictory ownership, but independent implementation/test work may continue elsewhere.

Disable/removal stops admission, completes existing merges, checkpoints other work and retains authoritative history. Restore only configuration still owned by this installation; concurrent edits must be preserved. Remove the integration from the selected scope and test that a remaining scope is reported accurately. Uninstall is not authorization to erase unfinished worktrees, databases or history. Purge is a separate explicit action.

### Delivery contents

The final product delivery includes Git commit and pushed branch, exact package hashes/versions, locked dependencies, MIT license, standalone host packages, reproducible build instructions, verified installation/update/disable/removal commands, project integration patch, complete cutover/recovery instructions, current task-management guide, generated transition reference, source inventory, state export/import tools and operator diagnostics.

Keep full private run traces and credentials outside the public repository. Commit test code and safe fixtures; publish only redacted summaries and distributable artifacts. Scan the staged tree and final artifacts for secrets, personal names in Markdown, copied user paths, fixture credentials and private source snapshots before an authorized push. Do not push raw application snapshots or local authentication/configuration files.

## Risks and open decisions

Material product choices are resolved in the recorded user decisions. Both installed hosts are required. macOS is the primary execution target; real Linux-container portability and CLI smoke coverage do not establish native Linux services or desktop integration.

Use the Test Plan (TP)'s entry criteria for identity, host, resource, cost and receiver prerequisites. The Test Status Report (TSR) owns current proof and blockers, including the missing second Linear identity. Continue independent work when a dependent capability is unavailable; do not guess a pass or omit required coverage.

Lifted total ceilings do not establish completion. At a failed guard or interrupted batch, preserve code, exact evidence, remaining failures and recovery state. Continue independent authorized work where safe and report incomplete integration readiness until every required gate passes; never reduce scope or erase a failed attempt.
