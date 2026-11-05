# AI task manager implementation plan

Status: Approved Implementation Plan (IP); implementation and acceptance are tracked separately in the Test Status Report (TSR), [test_status_report.md](test_status_report.md).
Sources: [Policy (POL)](policy.md), [Spec Driven Development document (SDD)](spec_driven_development.md), [High Level Document (HLD)](high_level_document.md), and `config/execution-policy.json`.

## Goal and specification

Build an installable task coordinator for Claude Code and Codex CLI. Deliver the approved behavior, self-contained packages and evidence that every required acceptance cell passes. A written implementation or a passing component suite alone does not satisfy the goal.

The Policy (POL) owns authority and roles; the Spec Driven Development document (SDD) owns lifecycle, merge and failure/receipt rules. The High Level Document (HLD) owns architecture. The Test Plan (TP), [test_plan.md](test_plan.md), owns methods and exit criteria. This plan owns delivery order and recovery preparation. Preserve requirement/case identities and historical results; version changed expected behavior from approved product principles instead of freezing legacy procedures as requirements.

The build has no overall spending, elapsed-time or retry ceiling. `config/execution-policy.json` records this authorization, finite execution controls, publication boundaries, MIT holder and required host/model profiles. Preserve the original run, accounting and every attempt through restarts; normal-session activation, public release and production changes retain their separate review boundaries.

### Required scope

Build the standalone system through its own interfaces and focused domains. Do not provide the retired merge shell entry point or its wrappers/aliases. Bind implementation and verification to the approved contracts. The following table defines required outcomes; it does not claim that they are implemented.

| Work | Required result and verification |
| --- | --- |
| Full merge refactor | Separate orchestration, Git publication, checks/cache, worktree/database lifecycle and reporting into focused local domains. Define and verify the new product merge API/CLI contract. Do not ship `merge-to-dev.sh`, a wrapper or an alias for it. Critically map source scenarios to retained outcomes, corrected behavior or retirement; preserve useful safeguards, owned commits and truthful results. Verify new behavior in independently authored disposable projects. Separate network services need an actual lifecycle or deployment reason. |
| History and performance audit | Start with P-CG-64, Merge Queue Reliability, then inspect relevant CG tasks across statuses, including descriptions, full comments/replies and linked issues. Include comment-only relevance. Distinguish capture from semantic review, reported claims from verified source, and actual reproduction from inference. Trace commit hooks, checks/tests, queue waits, rebases, database setup/cleanup, redundant work and fault attribution. Record separate timings for waiting, checks, database setup, commit and publication. Investigate the reported hour-long path for isolated 5–10-line changes; distinguish measured causes from issue reports and source inference. Target a few minutes for representative low-impact changes on the recorded environment, with benchmark cases and budgets frozen before tuning. Use impact-based checks and provenance-bound reuse; line count alone cannot waive checks. Verify broad-impact, cold/warm and concurrent cases as well. Cover dev advancing during a wait, invalidated check receipts and a database released before its last possible consumer. |
| Complete migration cleanup | The future How-to Guide (HTG), `migrate.md`, must drive a dry-run manifest for superseded instructions, Linear/agent scripts, hooks/jobs, tests, entry points and local task stores. Import required state, fence old writers, verify all callers use replacements, remove only matched obsolete artifacts, then install/activate one authority. Retire obsolete unfinished Linear issues through supported terminal states with reasons/replacement links; retain completed/delivery history and unrelated work. Test interruption and recovery before application use. |
| Decision requests | Add `Waiting for Decision` and a real author mention only when a human choice is required. Use the current Technical Writer Decision Record (DR) template: context, options, recommendation and rationale. If the creator is an agent, target the assigned human. Routine notices do not qualify. Scripts must track requests, deduplicate comments, preserve unrelated labels, handle partial remote outcomes and clear the managed label only when no decision remains unresolved. |
| Dynamic agent delegation | Keep the human Linear principal as assignee and resolve the agent delegate from the verified workspace/session connection. Use stable IDs rather than a hardcoded name; require setup selection when identity is ambiguous. Distinguish creator, human assignee, integration actor, Linear delegate and actual local worker. Revalidate permissions/delegation and test revoked access, reassignment, account changes and competing sessions. Delegation metadata does not prove an agent process or Agent Session exists. |
| Testing authority | The approved plan already authorizes scoped tests and necessary retries. Workers review their code/resources, freeze criteria and run finite measured local batches without repeatedly asking the user. Lead source/result review precedes integration. New permissions, credentials, external resource classes and normal activation retain their existing boundaries. |

### Project independence and final migration

Build and verify the product so it installs and works in an empty project. AI Index supplies read-only scenario evidence, regression lessons and mistakes to assess critically. Its scripts, instructions, architecture and command surfaces do not define the new methodology. The standalone product owns its merge interface; the retired shell entry point is not a compatibility obligation.

Treat AI Index’s existing worktree, database, agent and Linear workflow as potentially defective. A flow working there does not establish that its design, sequencing or policy is correct. Define the standalone workflow from the approved product principles and critically decide which scenarios or safeguards to retain, correct or reject. Regression tests must verify those intended outcomes and failure/recovery cases, rather than freeze legacy mistakes as required behavior.

The product must not depend on AI Index, its checkout, workflow scripts or translations of its old check catalog. No AI Index script may live anywhere in the product repository, including ignored caches, worktrees, examples or regression fixtures. Inspect originals directly, read-only. Remove existing product-side copies through a reviewed ownership/provenance manifest. Keep any required immutable historical source evidence outside the product tree with its hashes and relocation record; create no symlink or import bridge. Author product fixtures independently from intended scenarios.

After the product is complete, the user will request a fresh audit of the then-current AI Index. Inventory all existing files and Markdown instructions, trace relevant callers, and report what the migration will remove, edit, replace or retain, with unresolved items explicit. Delete an entire file when its responsibility is fully superseded; remove only overlapping sections from mixed files and preserve unrelated behavior. Obsolete instructions are removed. Author new product-owned integration guidance only where needed; no old task-management filename or path is a required survivor.

The final migration report must explain the reason and replacement owner for each change, what the script handles, and what the user must change in relevant Markdown files. Prepare tested commands and recovery in `docs/migrate.md`. Fence old writers and remove superseded responsibilities before installing and activating one new authority. No overlapping task, Linear, agent, worktree or merge control system may remain active.

The current AI Index checkout remains read-only throughout this build. Final audit, removal and integration are later user-requested work, not permission to edit it now. Present any further audit-driven change to the agreed methodology or scope for user approval before incorporating it into maintained plans.

## Current context and approach

Use the maintained architecture and requirement inventory to assess existing product code before assigning a phase task. Record what is implemented, what is verified and what is missing. Reuse accepted source-bound evidence; a new delivery sequence is not a reason to rebuild working code. Reinspect the then-current original application read-only before any separately requested target integration.

Scenario analysis covers Linear status/synchronization, issue intake, agent admission, worktrees/databases, merge and instructions. Use findings to identify intended outcomes and faults. Application source and its test harness are evidence to inspect, not product runtime, a required schema or an implementation to reuse.

Keep untested connector writes and killed-session behavior visible as coverage gaps. Build independent product cases for the intended outcomes. Test and clean only verified run-owned TST resources after verdict capture; never run team-wide cleanup or adopt an old cleanup script as a product procedure.

Use the product architecture and independently authored fixtures. Do not import, copy or invoke the old synchronizer or other AI Index scripts to implement product behavior. Skill Builder produces independently installable skill repositories outside this checkout; assemble verified pinned artifacts into the product. Keep both source skills unchanged.

### Existing-issue intake

Existing-issue intake uses the focused `task_find`, `task_inspect` and `task_admit` operations. The frozen contract and 84 case families are retained under `tests/existing/frozen/`. They extend the existing acceptance work without replacing R01–R38 or the global case inventory.

The provider reader verifies metadata scope before retrieving prose, then checks the complete state timeline and a fresh revision. The core binds reviewed exact content and a separately observed lead selection to a transactional canonical claim. Admission, queue and each startup phase obtain a separate complete reread and a 30-second receipt bound to the consuming operation, task/version, decision revision and enrolled store. Core verifies that context at the transaction boundary; it cannot substitute a newer version for the caller's expected version. A committed same-key retry preserves its earlier result without another effect.

A new remote Todo binding remains unqueued until the existing enqueue authorization confirms session membership. Prose confirmation or remote settlement cannot establish imported queue membership. Queue/start checks and permanent delivery/terminal fences share core rules; positive delivery also guards new prose authority and linked-parent eligibility. Lookup does not rewrite cached Task flags. No remote issue is created as a lookup fallback.

The additive schema-3 migration preserves task/outbox/resource history and introduces stable store identity and lifecycle records. The installer must explicitly call the trusted claim-store enrollment port before product composition; ordinary runtime configuration cannot enroll a replacement. A copied or replaced store is refused even at the old pathname. Same-file renames can retain enrollment after an explicit configuration update. Restore/replacement integration still needs verified quiescence and retained ownership; this task supplies no automatic restore command.

Integrate the implemented selection-receipt contract with the actual host observer before claiming the live selection path. Delivered-task follow-up submission uses an explicitly enrolled capability and exact approved prose; uncommitted retries revalidate the original enrollment and configuration. Keep complete installation/cutover, project-neutral resource/merge interfaces and live end-to-end receipt work visible as dependencies. Installed CLI/MCP tests with authored loopback transport establish their mechanical boundary, not those live paths. Exact observations and external-conflict fencing bound the remaining cross-system race; SQLite cannot lock Linear against a later edit.

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
| Product-owned resource and Git interfaces | Implement and verify standalone worktree, database, check and merge capabilities with owned fixtures and trusted configuration. Original application scripts may inform scenario analysis but are not product dependencies or executable test mechanisms. |
| Technical Writer and Skill Builder | Document quality and skill production/evaluation. Use their installed environments, typed runners and actual version hashes; do not guess dependencies or claim fresh behavior from replay. |

The table describes checks to perform, not current availability. `config/research-sources.json` retains preparation-time versions and observations; consult the Test Status Report (TSR) for later evidence. Inspect installed help/current official contracts before executing version-sensitive commands. Authentication alone does not prove model execution, and a local fixture does not prove live acceptance.

Use the explicitly selected Claude account in `config/execution-policy.json`. Launch the unmodified `claude` executable with that approved configuration; refuse account fallback. The Test Plan (TP) owns credential handling, fixture scope and required distinct-user checks.

## Work sequence

Complete P00 → P01 → P02 → P03 → P04 → P05 → P06 → P07 → P08 → P09 → P10 in that exact order. Finish the merge work inside P03 before advancing to P04. Only one phase is active. Parallel workers may own independent items within that phase; they may not open a later phase objective.

This is incremental assembly of the existing product. For each phase, inspect its exact required items and reuse valid code, packages and source-bound evidence. Mark each item complete, partial, blocked or not yet assessed. Implement only missing or defective work. Do not reset the repository, erase failures or rebuild accepted components merely because the scheduling method changed.

An already-built later-phase component may support the active phase's required path. A dependency defect may be corrected only as a bounded task needed to close that path; it does not authorize unrelated later-phase work or count the later phase complete. If a required capability or gate creates an unresolved ordering conflict, expose that conflict against the governing requirements. Do not substitute mocks for required genuine evidence or weaken a gate to advance.

The machine-readable work graph, `config/work-items.json`, retains its 11 phase gates and 34 work items. T01–T04 provide prerequisites and reviewed contracts. The original T01–T34 work items and requirement/case identities remain traceable. Each assignment names one active phase item, its owned paths, intended usable result and relevant checks. The required-item tables below are the reporting checklist; no separate module score or invented task list overrides them.

### Definition of done for a phase

A phase is complete only when all of its exact required items and its detailed exit conditions are proved on the maintained candidate:

1. Each item has its promised artifact or implemented behavior, with scope and interfaces matching the governing documents.
2. Its supported command, procedure or demonstration produces the required observable result. Preparation, a commit, a test count or a receipt alone does not establish that result.
3. All applicable checks pass, including required failure/recovery and available incoming connections. Actual host/provider/PTY/platform evidence remains mandatory wherever specified.
4. Relevant complete changes have accepted independent review and are integrated. Reuse unchanged accepted review and results; resolve findings that prevent the promised behavior.
5. The completion record identifies exact item IDs, source/package versions, commands, observed states, failures, review and owned cleanup. Unknown, blocked or failed obligations remain incomplete.
6. The next phase receives a runnable cumulative result and explicit retained limitations outside the completed scope. Later-phase acceptance is neither claimed early nor silently omitted.

Record the phase exit against those facts before admitting the next phase. A ceremonial additional review is not required when matching accepted evidence already proves the exit. If later work invalidates an earlier result, reopen the affected item, repair and reverify it; do not discard unrelated accepted evidence or claim the regression never happened.

After each exact item is completed and verified, prefix its deliverable name in this plan with **DONE -** and add its completion evidence in that row's Status / evidence cell. Preserve the item ID and original deliverable wording. Link a dated summary in the Test Status Report (TSR) with the source/package version, observed result, relevant checks, accepted review and owned cleanup; retain full private evidence separately. Update the plan before admitting the next item. Mark the phase complete only after every required item and its exit result are proved. A source file existing or a partial component passing does not justify DONE.

### Delivery rounds and continuation

Each bounded implementation round starts with one active phase item, the missing behavior, expected usable result, source/environment fingerprint and affected checks. Name its exact remaining blockers. Assign work that closes those blockers; integrate accepted changes promptly and prove the cumulative result before advancing the primary focus. Close those blockers before opening another workstream. Parallel assignments are eligible only when they directly advance or verify the current increment through settled interfaces. Keep other implementation objectives queued until the reviewed, integrated increment demonstrates its required working behavior. Finish, independently review, integrate and verify that part before opening another independent objective. A partial round may deliver an accepted subpart, but it cannot mark the whole item complete. Every round records the actual delivered implementation or the exact failure and next correction.

Reconcile real workers, processes, tasks and Git before admission. Use one task, worktree and disposable database per worker. Confirm one actual ACK and task In Progress, then immediately send START. Use the actual host/controller capacity for independent active-phase work only. Consume finished handoffs promptly, settle their ownership and start the next eligible same-phase task; a handoff or status answer is not a stopping point.

Run already-approved affected checks in finite measured batches without another permission queue. Preserve original grants and deadlines. Reuse valid source-bound evidence; repeat only for changed relevant source, an unresolved finding or a required acceptance case. Combine related blocking repairs into a coherent candidate before an expensive installed demonstration. Do not create repeated preparation/review queues or broaden each patch into a full unchanged suite.

Use the existing progress guard before additional preparation/review/tests. Two unchanged failures require diagnosis and a changed approach or eligible active-phase dependency. Two reporting intervals with no verified implementation or required-acceptance advance are Stalled, even when workers are busy. Name the bottleneck and owner, change the action and state the next observable result. Stop that queue, identify the smallest remaining blocker, assign its correction and make the connected demonstration the next result. Do not respond with another planning framework, expanded scope, repeated successful review or relaxed acceptance. Do not rename a retry, lower criteria or inflate completion. Inspect healthy bounded checks and measured waiting rather than restarting them blindly.

Before ending execution, reconcile a fresh worker snapshot and the continuation guard, including pending START messages and unconsumed handoffs. Actual completion, an explicit user pause or solely external prerequisites after independent active-phase work is exhausted may justify stopping. The automation is a watchdog; execution advances on worker completion rather than waiting for the next scheduled report. Neither a prompt nor a private guard can guarantee host availability.

### Phase-item progress reporting

Every 20 minutes, send one table using the exact required-item IDs and wording below. Include P00–P10 summaries and the active phase's individual items; show P03 merge work under P03. Columns are **Phase / exact item and unit**, **Previous completion**, **Current completion**, **Change since last report**, **Done since last report**, and **Next action / blocker and owner**.

For an assessed phase, completion is its completed required items divided by its fixed required-item total. Partial items are not whole completions. A child repair or component row does not add an extra item to the parent inventory. A repair does not create another item or increase the denominator. These are equal checklist items, not equal effort or a remaining-time estimate. Do not average submodule percentages as an effort estimate. Show `X% – x/y`, percentage-point and item-count changes, and the actual behavior or artifact delivered. For unassessed existing work or missing historical values, show Unknown rather than assuming no implementation exists. Never use the four workflow stages—implementation, checks, review and integration—as a product-completion percentage.

These group existing responsibilities; they do not introduce new components or requirements. The item tables declare Preparation, Implementation or Acceptance. Whole-product implementation completion counts the non-overlapping Implementation items only; setup/document-planning outputs and final acceptance results remain required but cannot inflate that percentage. Count components when components are the declared outputs, submodules when submodules are the outputs, and files only when the files themselves are required deliverables. Do not count files touched, commits, tests or elapsed time. Do not average different-unit percentages or double-count an item and its children. Freeze the table-derived inventory before scoring; disclose an approved inventory change and retain earlier reports without inventing a comparable percentage.

Keep live Linear, each of the four required host/model profiles, Linux, endurance and final combined acceptance separately identified in the same table. They do not become implementation points. State None when nothing finished and report measured implementation/test/wait durations or Unknown. Name workers waiting on the lead and resolve avoidable waits before more preparation.

Compare with the most recent table actually delivered. Save its timestamp, row values, checklist version, evidence and verdict. Begin preparing after 17 minutes so delivery finishes within 20; settle urgent owned process obligations first. Then assess Advancing, At risk, Stalled or Externally blocked and take the next eligible active-phase action. A metric change, planning artifact or scheduled wake alone is not product implementation. Do not copy stale work-item labels or sum the changing list of repair assignments. Keep coordination reasoning short and act on the next known eligible step. Preserve P00–P10 and merge-gate criteria, denominators and evidence in the durable release ledger; changing presentation cannot remove a release obligation.

### Required phase work and exit results

The following sections are the implementation order, not a deferred release checklist. Each required-item table names the phase outputs and the evidence needed to complete them. The detailed instructions and gates remain binding. The architecture's module and connection requirements are retained below as coverage references.

### P00 — Bootstrap and verify prerequisites

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P00.1 | DONE - Repository and isolated workspace access | Preparation | Verify the product repository, current Git state, authorized remote and isolated worktree/fixture access without losing existing work. | 2026-11-05 — [Verified completion](test_status_report.md#p00-1) |
| P00.2 | DONE - Preserved build controller and execution ledger | Preparation | Verify schema/policy binding, original history, capacity, finite invocation bounds and accounting through the existing controller. | 2026-11-05 — [Verified completion](test_status_report.md#p00-2) |
| P00.3 | DONE - Runtime, dependency and resource readiness | Preparation | Verify the tools and small resource probes declared below in the actual execution environment; retain exact versions and unavailable dependencies. | 2026-11-05 — [Verified readiness and limits](test_status_report.md#p00-3) |
| P00.4 | DONE - Selected accounts and scoped test identities | Preparation | Verify selected CLI authentication/model-access evidence and both distinct Linear identities in their authorized scope; do not confuse preflight with workflow acceptance. | 2026-11-05 — [Verified access and limits](test_status_report.md#p00-4) |
| P00.5 | DONE - Trusted execution configuration and resumable checkpoint | Preparation | Bind current authority, accounts, source/requirement versions, process ownership and exact next action in a durable checkpoint. | 2026-11-05 — [Verified completion](test_status_report.md#p00-5) |


Read the approved product policy and maintained sources. Resolve `PRODUCT_REPO` in the separate build workspace and inspect existing content before acting. An empty-project build/install must not require the AI Index checkout or its instructions. Read-only historical analysis may separately discover `SOURCE_REPO`; that optional evidence source grants no execution or modification authority. Clone only the authorized product repository using structured arguments and preserve unrelated work.

Use the recorded private build ledger and preserve its run ID, UTC start, accounting, attempts, source/requirement hashes and policy amendments. Build-only operational records stay in their declared ignored storage and are excluded from installed artifacts; they are distinct from installed product state. Do not move or reset the existing ledger as part of source-copy cleanup. Continue finite measured reservations and per-invocation bounds.

The bootstrap may use the existing host's task/delegation tools and a deterministic build ledger until the new engine passes its gates. This is development scaffolding, not a claim that the new product is already enforcing assignments. Register development work in the run's TST project through an audited isolated path. Workers still receive one task and separate worktrees and fixture databases. Keep bootstrap scheduling separate from product policy; replace scaffolding only after its replacement is tested.

Prove authentication, file access, model access, network, Docker, disk/memory headroom, worktree creation and a tiny disposable database operation. Run dependency probes in the same environment and permissions as the eventual cases. Resolve host approvals and hook trust once in the intended configuration; do not suppress host safeguards to make tests pass. Missing capabilities block only dependent work and remain explicit in the final result.

P00 uses the explicitly selected Claude account for authentication and authorized model-access probes. Verify the actual account before new effects. Retain previous account proofs as historical; do not rebind them to the newly selected account. Both-host acceptance still requires actual tool-enabled evidence.

Gate: trustworthy environment inventory, nonempty authorization/limit record, working policy, cost and capacity admission with per-invocation deadlines and a durable checkpoint. Verify the primary test identity before live TST writes. A missing secondary Linear identity blocks two-user cases only; missing Claude access blocks Claude execution tests only. Keep independent core/mechanical and available Codex work eligible. Do not start an unmetered paid batch.

### P01 — Freeze contracts and baseline

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P01.1 | DONE - Critically reviewed scenario and regression inventory | Preparation | Reconcile existing read-only source/history analysis, including relevant descriptions/comments, CG-3589 and CG-3596; classify retained, corrected and retired outcomes. | 2026-11-05 — [Verified scenario dispositions](test_status_report.md#p01-1) |
| P01.2 | DONE - Product workflow and interface contracts | Preparation | Freeze standalone task, status, agent/resource, merge and receipt contracts without retired compatibility obligations. | 2026-11-05 — [Verified contract completion](test_status_report.md#p01-2) |
| P01.3 | DONE - Requirement, work-item and acceptance traceability | Preparation | Map every approved requirement and case to its implementation owner and phase; preserve original identifiers and dependencies. | 2026-11-05 — [Verified contract completion](test_status_report.md#p01-3) |
| P01.4 | DONE - Independent baseline and regression fixtures | Preparation | Reuse source-bound baseline results and independently authored fixtures; retain failures and distinguish measurements from reported claims. | 2026-11-05 — [Verified baseline and fixture provenance](test_status_report.md#p01-4) |
| P01.5 | DONE - Consistent maintained instructions and delivery contracts | Preparation | Resolve contradictory planning/caller instructions within product scope; bind the governing documents and phase/item inventory. | 2026-11-05 — [Verified contract completion](test_status_report.md#p01-5) |


Inspect previous workflow rules and callers only to extract critically reviewed scenarios and prepare the later target-specific removal inventory. Include task/agent instructions, Linear writers, resources, hooks, timers, watchers, background processes, pipeline callers, connectors and nested settings. Trace actual consumers and dynamic configuration. Treat old rules and implementations as evidence; the approved product requirements determine authority.

For the later migration, map each relevant rule/caller to its source/hash, intended outcome, retain/correct/retire decision, new owner, state to import, fencing/removal action and verification. Preserve a command surface only when the user still requires it; the old merge shell entry point is retired. Resolve every intersecting row before target activation, with wholly superseded files removed and only overlapping sections removed from mixed files.

Implement T34 in its normal dependency order before packaging, using the responsibility map in the High Level Document (HLD) and `config/repository-layout.json`. Move each declared source and its consumers together, preserving before-image hashes, historical paths, command names and arguments. Keep prose, editable/rendered diagrams and screenshots under `docs/`; configuration, tools, acceptance oracles, published indexes and prototypes use their declared homes. Run `python3 tools/repository_layout.py`, the relocated handoff checker and focused ownership/consumer tests. Stop any model or helper bound to the old paths before integrating the relocation; the lead updates current private consumers at that boundary. Preserve the coherent commit and verified before-images for collision-checked recovery. Installed-package and whole-product acceptance remain later gates; folder separation does not require network services.

Expand acceptance from the approved standalone policy, its complete status-pair/cause rules and critically reviewed scenario inventory. Freeze actual fixtures, negative controls, fault seams and expected outcomes before tuning. Preserve earlier case IDs/results as history and version intentional corrections explicitly. Do not use a legacy algorithm or its output as the correctness oracle.

Build and run independently authored product fixtures and test procedures in owned disposable projects. Retain historical baseline failures and unrun source scenarios without copying or executing the old harness. Gate: reviewed standalone contracts, reproducible fixtures, explicit source-derived coverage and a complete implementation responsibility map. Later target-specific inventory closure remains a separate cutover obligation.

### P02 — Implement core records and admission

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P02.1 | DONE - Typed task records and scoped application API | Implementation | Implement and verify task/domain records, canonical claims and one shared policy through supported operations. | 2026-11-05 — [Verified Core completion](test_status_report.md#p02-1) |
| P02.2 | DONE - Durable metadata, migrations and enrollment | Implementation | Verify transactional history, identity-preserving schema changes, enrolled-store checks and refusal of copied/replaced stores. | 2026-11-05 — [Verified Core completion](test_status_report.md#p02-2) |
| P02.3 | DONE - Lifecycle, writing and authority guards | Implementation | Enforce all required transitions, human holds, delivered/terminal fences and exact approved prose without an alternate writer. | 2026-11-05 — [Verified Core completion](test_status_report.md#p02-3) |
| P02.4 | DONE - Core CLI and MCP operations | Implementation | Exercise supported create/read/admit/queue and policy operations against the same API; declared external fixtures remain limited to their stated scope. | 2026-11-05 — [Verified Core completion](test_status_report.md#p02-4) |
| P02.5 | DONE - Atomic admission, idempotence and core recovery | Implementation | Verify concurrent canonical claims/capacity, exact-key replay, denied scope and restart without lost or duplicate core operations. | 2026-11-05 — [Verified Core completion](test_status_report.md#p02-5) |


Implement pure domain rules and one application API. Separate task intent, execution attempt, worker identity, resource owner, delivery evidence, human decision and synchronization operation. Keep domain imports independent of providers and presentation. Enforce boundaries with executable import checks.

Implement schema-versioned SQLite storage, optimistic versions, unique claims, atomic capacity reservations, append-only audit events and durable outbound intents. External process/API actions do not participate in the SQLite transaction: reserve, perform, confirm and compensate/reconcile explicitly. A schema migration must preserve a before-image and have a tested recovery path.

Gate: all pure policy, scope, transition-pair, transaction and architecture tests pass on exact artifacts. Invalid or premature actions change no unauthorized state.

Before merge integration, freeze the standalone merge contract and critically reviewed retained/corrected scenario expectations. Audit relevant source/history as evidence, preserve failures and verify concurrency/recovery in independent fixtures. Neither the old script, its byte layout nor a compatibility alias is required.

### P03 — Implement workers, resources and the complete delivery path

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P03.1 | Owned worker attempts and isolated environments | Implementation | Bind each task to one real observed worker, branch/worktree and declared development database; enforce startup ordering and ownership. | Partial — Installed local worker/isolation control integrated; genuine worker evidence remains open. [Current evidence](test_status_report.md#p03-current-blockers-20261105). |
| P03.2 | Worktree and database lifecycle | Implementation | Implement allocation, retention, release/reacquisition and owned cleanup, including unknown ownership and exhausted capacity. | Partial — Resource and consumer fences integrated; concurrency/reacquisition workload remains open. [Current evidence](test_status_report.md#p03-current-blockers-20261105). |
| P03.3 | Worker supervision and stop reconciliation | Implementation | Observe actual progress/failure/exit, distinguish stopped from unknown, and recover hold/cancel/interruption without competing attempts. | Partial — Shutdown reconciliation integrated; normal stopped-owner continuation is blocked by OS-OWNER-TAKEOVER-1. [Current evidence](test_status_report.md#p03-current-blockers-20261105). |
| P03.4 | Deterministic workflow run and resume | Implementation | Deliver the script-owned controller over saved task/runtime/provider facts, with omission/order/scope and restart controls. | Partial — Installed same-key reconciliation integrated; a distinct owner process cannot yet resume retained work. [Current evidence](test_status_report.md#p03-current-blockers-20261105). |
| P03.5 | Product-owned review, commit and merge interface | Implementation | Connect worker-owned commits, independent exact-candidate review and one merge authority to the trusted target through the new API/CLI. | Partial — Current-target native review, commit and merge connection integrated and verified; genuine delivery and owner continuation remain open. [Current evidence](test_status_report.md#p03-recovery-review-components). |
| P03.6 | Merge checks, resource recovery and stage timings | Implementation | Verify moving targets, concurrency, conflicts, provenance-bound check reuse, last-consumer resource lifetime and measured small/broad/cold/warm cases. | Partial — Merge-turn/recovery controls and small/broad cold-warm measurements verified; cumulative queue wait exceeds the frozen budget and database-stage timing remains open. [Current evidence](test_status_report.md#p03-recovery-review-components). |
| P03.7 | Complete supported one-worker delivery path | Implementation | Demonstrate registration through owned work, committed fixture change, standalone merge, authoritative readback/reporting and checked cleanup; retain actual-host/provider obligations. | Partial — Installed local code cycle integrated with explicit same-key reconciliation; genuine task cycle and owner restart remain open. [Current evidence](test_status_report.md#p03-current-blockers-20261105). |


#### Completed P03 components

These completed portions retain their exact required-item parents. They add no scoring units; P03.1–P03.7 remain open until each complete criterion is verified.

* DONE - Application database alias refusal, verified 5 November 2026. Owned SQLite resources refuse shared physical links and recheck ownership before removal. [Dated completion evidence](test_status_report.md#p03-resource-alias). Remaining P03.2 lifecycle and consumer-boundary work stays required.
* DONE - Exact prepared-candidate review fence, verified 5 November 2026. Publication requires independent scope- and expiry-bound review of the prepared commit/tree. [Dated completion evidence](test_status_report.md#p03-candidate-review). Installed/native producer and P03.5/P03.7 connection work stays required.


* DONE - Worker commit and code-unit verification interfaces, integrated 5 November 2026. Owned commits, scoped independent review records and verification units connect through the supported controller. [Completion evidence](test_status_report.md#p03-integrated-code-components).
* DONE - Normal sealed code and two-stage review provisioning, integrated 5 November 2026. The default resolver constructs code capabilities and distinct worker/candidate reviewer slots from trusted configuration; uncovered check inputs refuse before effects. [Completion evidence](test_status_report.md#p03-integrated-code-components).
* DONE - Dependency observation and publication authority handoff, integrated 5 November 2026. The controller consumes exact current preparations, bounds conflict recollection and hands off to the canonical publication context without reviving expired authority. [Completion evidence](test_status_report.md#p03-integrated-code-components).
* DONE - Native shutdown cancellation and first-outcome stop retention, integrated 5 November 2026. Concurrent close calls preserve the exact first physical stop result, including uncertainty. [Completion evidence](test_status_report.md#p03-integrated-code-components).
* DONE - Installed code-delivery control with explicit same-key reconciliation, integrated 5 November 2026. The wheel-bound normal path performs both reviews, checks, merge and readback with one execution per role. This uses declared local native/provider controls; genuine acceptance remains open. [Completion evidence](test_status_report.md#p03-integrated-code-components).


* DONE - Current-target native review and bounded reviewer allocation, integrated and connected 5 November 2026. Distinct preapproved reviewer slots retain exact candidate bindings across supported target succession and replay. The current-source two-case connection passes; genuine execution remains required. [Completion evidence](test_status_report.md#p03-recovery-review-components).
* DONE - Quiescent target succession and retained merge-turn ownership, integrated 5 November 2026. Successor claim recovery, publication-context replay, executable routing checks and publication-child turn retention have accepted source review and focused controls. The four-task workload demonstrates delivery and exact-key recovery, while its queue latency budget remains unmet. [Completion evidence](test_status_report.md#p03-recovery-review-components). This portion does not complete P03.6.


Prepare owned worktrees and required databases with durable identifiers, confined paths and compensation for partial setup. Use separate disposable SQLite fixtures for product-development tasks. Exercise other configured database providers with independently authored resources; the old application stack/scripts are not a standalone prerequisite. Control metadata is never a worker application database. Verify target-specific resource integration only in the later authorized target audit/cutover.

Launch and supervise actual worker processes, recording PID plus process-start identity, host session/thread ID and attempt capability. Distinguish startup, liveness, progress and exit. A heartbeat is not progress; a worker message is not proof of success. The startup handshake must prevent task execution before the task and remote assignment are confirmed.

Prove one real task through registration, an observed worker, isolated owned resources, a committed fixture change, the new product merge engine and checked cleanup. Reconcile final Git and Linear facts. Add refusal and crash cases at every effect boundary. Gate: the complete standalone one-worker path and recovery work before increasing concurrency.

#### Deterministic run and resume controller

Implement R15 revision 2 as a script-owned loop over saved Core, provider, runtime and operation facts. Keep workflow intent/claim history separate from Core task status. Start with a bounded non-code path using existing guarded worker, resource, verification and synchronization ports. Missing integration or genuine semantic acceptance produces a named wait; it cannot produce success.

Freeze omission, wrong-order/file/scope, concurrent-claim, human-change, lost-reply and lineage cases before implementation. Verify a real owned local worker and resources, durable restart without duplicate effects, and preserved partial work. Do not import the private report evaluator or its answers. Then connect public CLI/host invocation and code delivery through their owners, and run the actual-host omission cells. Component tests do not satisfy those later gates.

### P04 — Implement Linear policy and live integration

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P04.1 | Approved new-issue and existing-issue intake | Implementation | Create approved Backlog work or adopt an eligible existing issue, then explicitly queue it without duplicate creation or hidden queue membership. | Not assessed |
| P04.2 | Durable outbound synchronization and confirmation | Implementation | Persist intent, apply authorized remote changes, reconcile uncertain/lost replies and confirm exact authoritative readback. | Not assessed |
| P04.3 | Real-time inbound events and reconciliation | Implementation | Deliver the installed signed-event path and approved reconciliation behavior with replay/order/echo protection, restart and actual owned endpoint evidence. | Not assessed |
| P04.4 | Human assignment, dynamic delegation and decisions | Implementation | Verify human assignee, connected-agent resolution, permission changes and decision-only labels/mentions using approved prose. | Not assessed |
| P04.5 | Lifecycle, conflict and dependency synchronization | Implementation | Handle all required status causes, holds, reassignments, dependencies, partial completion and concurrent human changes through core policy. | Not assessed |
| P04.6 | Live scoped isolation and fixture lifecycle | Implementation | Complete required real two-user/session cases and run-owned setup/cleanup, preserving foreign sentinels; all 18 live cases remain the phase exit requirement. | Not assessed |


Implement typed intake/queue/hold/resume/accept/cancel/duplicate/delivery operations. Apply the transition table, immutable delivered-issue start guard, human authority and verified non-code completion. Derive workflow-state IDs from the bound team; do not reuse state IDs between TST and the application team.

Use one durable synchronization writer. Record operation identity before remote requests, reconcile lost responses, read back mutations and report pending/failed synchronization separately from execution. A partial API response or missing history is uncertainty, not proof of an absent human decision. No blind retry of uncertain issue creation.

Drive the live TST scenarios, including distinct-user isolation, holds, dependencies, assignments, review, duplicate/cancel, partial issue completion and delivery stages. Use a real merge in the disposable copy; staging/production events are explicitly isolated fixtures. Gate: real readback agrees with local facts; negative scope attempts and concurrent-run cleanup leave sentinels unchanged.

### P05 — Integrate Claude Code and Codex CLI

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P05.1 | Shared installed host composition | Implementation | Compose the supported CLI/MCP operations, trusted configuration and actual plugin/skill discovery in isolated installed sessions. | Not assessed |
| P05.2 | Claude Code execution and managed-surface guards | Implementation | Complete actual scoped Claude workflows and refuse unmanaged state/agent/effect paths without account/model fallback. | Not assessed |
| P05.3 | Codex CLI execution and managed-surface guards | Implementation | Complete actual scoped Codex workflows with equivalent enforceable controls; unresolved proof blocks acceptance rather than enabling an unguarded mode. | Not assessed |
| P05.4 | Account, model, effort and invocation provenance | Implementation | Verify original finite grants, current identity, exact requested/observed profiles and actual tool/terminal events for each required host profile. | Not assessed |
| P05.5 | Host result, review and lead-report connections | Implementation | Prove actual worker result through independent review, confirmed CLI/Linear receipts, observed lead delivery, AI acknowledgement and user report, including relevant failures. | Not assessed |


Expose the same application operations through CLI and MCP. Install the actual package into isolated host sessions and verify skill discovery, MCP availability, hooks, argument schemas, error behavior and current host trust. Do not replace real host execution with a mock response.

Implement supervision around supported CLI structured events first. Use a supported persistent protocol only when required lifecycle control cannot be established through the CLI; record and test that choice. Capture actual final status and tool events, not just exit code or final prose. A shell process starting does not establish a successful model turn.

In strict managed sessions, block native task-state mutation and unmanaged subagent creation through every supported entry point. Test native agent tools, shell launch attempts, SDK/CLI paths, direct MCP calls and stale configurations. Engine capabilities must be checked server-side. General-purpose shell access is not a universal security sandbox: if an installed host cannot enforce the declared managed surfaces, mark that host integration incomplete instead of claiming an absolute anti-bypass guarantee.

Gate: both real CLIs complete a scoped installed smoke workflow and refuse unsupported state/agent actions. Neither host silently falls back to an unguarded mode.

Use the explicitly selected Claude profile for Claude host tests and Codex for its own host tests. Verify the approved account on every worker launch and refuse account fallback. Authentication preflight is not model-performance evidence. Every selected host/model/effort profile requires its own actual traces and complete required coverage.

### P06 — Complete scheduling and recovery

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P06.1 | Dependency, priority and starvation scheduling | Implementation | Implement cycle rejection, truthful blocker causes, explicit fulfillment and approved ordering without treating failed/canceled prerequisites as success. | Not assessed |
| P06.2 | Concurrent capacity and conflict admission | Implementation | Verify atomic reservations and declared file/migration/test/resource conflicts without duplicate assignments or invented work. | Not assessed |
| P06.3 | Integrated crash and interruption recovery | Implementation | Reconcile processes, Git, resources and remote operations before restart; preserve partial work, pending effects and foreign ownership. | Not assessed |
| P06.4 | Expired workflow and authority recovery | Implementation | Verify bounded successor/renewal behavior, historical identity and old-key refusal; no backfilled continuity or invented approval. | Not assessed |


Implement dependency-cycle rejection, exact blocker reasons, explicit dependency fulfillment, approved queue ordering and starvation visibility. Do not equate failed/canceled predecessors with dependency success. Honor project-specific file/migration/test-lock conflicts through adapters, without scheduling arbitrary team issues to keep workers busy.

Implement and exercise recovery through the fault matrix in the [Test Plan (TP)](test_plan.md#failure-and-recovery). Inspect actual processes, Git, databases and remote facts before restarting; unknown state prevents competing work.

Gate: the state-machine, process, fault and capacity tests preserve invariants with deterministic seeds and repeatable reproductions.

TST-144 implements the first expired-workflow recovery slice under R14/R15/R20/R36. Define and freeze [the zero-effect contract](spec_driven_development.md#expired-workflows-without-effects), migrate workflow identity without changing historical records, then verify trusted successor admission and old-key refusal. New admissions retain their original plan, task version and history bindings. Keep legacy enrollment and workflow evidence gaps blocked for separately authorized reconciliation; do not backfill continuity or expand this slice to partially effected work. Independent source review precedes integration, and the corrected enrollment prerequisite must be reconciled first.

### P07 — Implement the terminal views

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P07.1 | Scoped task projection and CLI/status output | Implementation | Render the current authorized session through shared JSON/text/status interfaces with truthful pending/conflict states. | Not assessed |
| P07.2 | Approved task sidebar controls | Implementation | Deliver grouping/counts, task activity icons, details, five-line preview/Show more, Hide/Show and whole-sidebar scrolling. | Not assessed |
| P07.3 | Native host layout and real terminal interaction | Implementation | Verify actual keyboard/mouse/focus/resize, accessibility and preserved host-native agent space through a real PTY. | Not assessed |
| P07.4 | View reconnect and guarded user actions | Implementation | Verify disconnect/reconnect and stale/duplicate controls through the shared guarded API without lost or duplicate mutations. | Not assessed |


Build scoped JSON/text CLI output first, then the right-hand companion pane and passive status line. Implement the approved grouping, activity, inspection, controls, scrolling and native-host layout from the [focused Spec Driven Development document (SDD)](terminal/spec_driven_development.md) through the shared scoped projection.

Use the maintained activity symbols and accessibility requirements in the [terminal specification](terminal/spec_driven_development.md#quality-and-interface-constraints). These are display conventions, not another workflow policy.

Gate: real PTY interaction and own-session isolation tests pass. A screenshot alone does not establish click or keyboard behavior. A fixed native host sidebar extension is not assumed.

### P08 — Build skills and run fresh behavior evaluations

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P08.1 | Versioned focused skill packages | Implementation | Use the current approved Skill Builder and Technical Writer to produce the required independently installable artifacts and declared responsibilities. | Not assessed |
| P08.2 | Generated policy references and usable guidance | Implementation | Bind commands, transition references and concise product instructions to executable contracts; remove obsolete duplicated authority. | Not assessed |
| P08.3 | Fresh behavior evaluation results | Acceptance | Run frozen mechanical and actual-AI cases, including omissions/wrong order and required host/profile coverage; retain every failure and unrun cell. | Not assessed |
| P08.4 | Installed skill discovery and operation | Implementation | Verify actual discovery, loaded instructions and repeatable supported workflows in both isolated hosts without source-checkout assembly. | Not assessed |


Use the current approved Technical Writer and Skill Builder versions, recorded by source hash. When a new version is supplied, audit affected work, revise what needs changing and rerun impacted checks while retaining prior evidence. Task prose must pass Technical Writer with the appropriate reference and Writing Style (WS) guide before publication.

Invoke the provided Skill Builder through its actual root `SKILL.md`. Use its installed dependency environment and typed start/packet/submit/evaluate/review/apply workflow. Keep its run records outside the skill repositories. Use the supplied MIT holder, focused domains, executable mechanisms and independent installation checks. Use impacted tests during iteration and the final relevant suites for the final candidate; the approved build authorizes complete acceptance through finite reviewed execution batches. Preserve source-skill managed state and all earlier closed packets.

Run the frozen host/profile evaluation matrix, repetitions and evidence controls in the [Test Plan (TP)](test_plan.md#fresh-behavior-evaluation). Gate: its required host/profile cells and relevant final checks pass on the exact candidate; missing observations remain blocking.


### P09 — Implement complete cutover and distribution

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P09.1 | Self-contained host packages and installation | Implementation | Build pinned distributable artifacts for both hosts and verify install/setup/doctor in empty and unrelated projects. | Not assessed |
| P09.2 | Update, disable, uninstall and state export | Implementation | Implement scoped lifecycle commands with owned before-images, retained authoritative history and no overwrite of foreign settings. | Not assessed |
| P09.3 | Generic migration and obsolete-writer fencing | Implementation | Preserve required task/decision/attempt/resource/pending-sync state, remove superseded responsibilities and enforce one authority. | Not assessed |
| P09.4 | Interrupted cutover and rollback recovery | Implementation | Rehearse interruption/restart and safe restoration or reconciliation after new writes, without dual writers or lost work. | Not assessed |
| P09.5 | Reproducible distribution and operator guidance | Implementation | Deliver verified build/lifecycle/recovery commands, hashes, licenses and necessary guides; target-specific AI Index audit and docs/migrate.md await the later user request. | Not assessed |


Implement generic installation, state migration and removal through the standalone product interfaces before normal activation. Remove superseded controls instead of wrapping them. Generate transition references from the executable policy and test parity. Keep product rationale and necessary operator guidance readable; no inherited instruction filename is mandatory.

At target cutover, delete obsolete workflow instruction files and remove only overlapping sections from mixed documents. Author concise new product-owned guidance only where actual consumers need it; no old task-management path must survive. Keep essential shared project instructions concise. Claude may use `@AGENTS.md`; verify actual imports and supported Codex discovery separately. Load detailed generated matrices and test history only when needed.

Build self-contained Claude and Codex packages from the same core and pinned skill artifacts. Native plugin formats and tool discovery may differ; test the installed artifacts, not just source directories. Implement install/doctor/update/disable/uninstall and state export/recovery. Resolve ownership and compare current settings before restoring any before-image. Do not store authoritative state in plugin caches or remove another integration's files.

Rehearse generic clean cutover in independently authored repository fixtures with tasks, holds, live workers, merges, pending sync, worktrees/databases and stale clients. Verify representative old-writer fencing without copying AI Index scripts. After standalone product acceptance, the user will request the fresh AI Index audit and target-specific adaptation/removal rehearsal. Neither stage permits dual live authorities.

After the user requests the final target audit, deliver `docs/migrate.md` with exact tested commands and an inventory-bound removal/edit manifest. Validate backups and prepared product artifacts, fence old writers, remove superseded responsibilities, install the new system, then verify one authority before activation. Use the new merge contract, with no retired shell entry point or alias. Test interruption, resumable recovery and rollback. Markdown guides the operator; scripts perform the changes.

Gate: all required records and contracts migrate; old writers are fenced; the new system alone passes installed acceptance; repeated install/remove and interrupted upgrades preserve data.

### P10 — Complete acceptance and prepare delivery

| Required item | Deliverable | Kind | Completion evidence | Status / evidence |
| --- | --- | --- | --- | --- |
| P10.1 | Final candidate combined-suite results | Acceptance | Run the complete required suite on exact final source/artifacts; no selected passing subset or historical component substitution. | Not assessed |
| P10.2 | Complete real Linear and host/profile evidence | Acceptance | Close every required real TST, installed host and four exact high-effort profile cell with reviewed source-bound traces. | Not assessed |
| P10.3 | Unrelated-project and Linux portability evidence | Acceptance | Verify the declared portable behavior in independent projects and real Linux environments without claiming unsupported desktop services. | Not assessed |
| P10.4 | Endurance, recovery and cutover acceptance | Acceptance | Complete required measured workloads/faults, including LP18 four genuine deliveries and 25,245 seconds, with ownership/isolation/cleanup. | Not assessed |
| P10.5 | Final delivery and release evidence | Acceptance | Verify all gates, source/package hashes, safe distributable contents, operator recovery and delivery records within existing publication/activation authority. | Not assessed |


Run the full final suite against the exact candidate artifacts, including live TST, installed host behavior, clean cutover, failure recovery, real terminal interaction, install/remove, second-project portability and Linux-container coverage. Fix failures through bounded, evidence-based iterations, then rerun impacted cases and the required final release checks. Preserve all attempts.

Run a measured mixed-workload endurance test with deterministic faults, multiple sessions, dependency chains, varied task sizes and restarts. Record the actual duration, workload, concurrency and invariant checks. Do not claim a 48-hour soak from a shorter test or treat the build's elapsed time as endurance evidence.

For the approved LP18 Option A, compose the separately versioned evaluator with the existing installed receiver, authentic retry observer and owned cleanup path. Require four genuine same-delivery/business-event requests and at least 25,245 seconds of continuously supervised operation; record provider intervals separately without inventing a scheduling tolerance. Preserve finite stage maxima, authority, freshness, two-human sequential isolation and once-only state/send checks. Qualify early-completion refusal, missing/extra/changed deliveries, duration and supervision failures with declared clocks; independently review the exact evaluator and current source bindings before a fresh finite live admission. Reconcile the narrow Test Plan (TP) criterion and packet fields first. Retain all earlier failures; no past run or local fixture establishes live LP18 acceptance.

Gate: the release checker accepts every required case/profile, source and package hash, migration row and evidence record. Produce the final evidence report, install/remove commands verified against the built version, operator recovery guide, known limits and integration patch. Normal-session activation remains a final review action.

### Autonomous operating procedure

Preserve the build controller’s schema/policy binding, ledger, start, charges, attempts and failures. A controller migration requires a fresh private backup, exact state/authorization binding, reconciled launch ownership and supported API review before execution.

Preserve the original build run and UTC start. Record the authorized policy amendment and its receipt without resetting elapsed usage, calls, spending or failures. The amended total ceilings are explicitly absent; keep finite measured capacity, reviewed batches and a monotonic timeout for every supervised invocation. A policy change or expired timer cannot establish that a worker stopped, release uncertain capacity or justify killing a merge blindly.

Choose finite invocation and spending allowances for each reviewed execution batch. Include workers, lead invocations, graders, retries and chargeable services in the same durable accounting. A later batch is an evidence-supported continuation, not a fresh run or a way to erase earlier usage. There is no overall retry-round ceiling under the recorded user authorization.

Use one atomic accounting ledger and finite batch reservations. Before a paid call, reserve a defensible maximum charge from current official pricing and enforced provider/transport limits; reconcile actual usage afterward. Parallel calls cannot reserve the same batch allowance or worker capacity. If a CLI transport cannot establish a bounded charge or usage provenance, use a supported bounded transport or block that paid call. Subscription sign-in alone does not establish unlimited allowance, zero incremental cost or a spending cap. Unpriced tools and new paid infrastructure remain unavailable.

Choose active-phase item and connection batches by dependencies, actual capacity and cost. The build runner schedules bounded model turns and deterministic commands, consumes their structured outcomes, persists a checkpoint, and resumes the next eligible task. A single interactive chat turn does not establish a continuously supervised background service. Persist child process identities, model turn IDs and job handles so loss of the chat does not create duplicate launches. If the host cannot sustain supervised background execution, report that preflight limitation before starting an unattended claim.

Use measured phase-item implementation and required connection progress to update forecasts and the eligible work queue. Report delays and remaining work without removing accepted requirements or treating an estimate as permission to bypass a gate.

At each completed work item and at least every five minutes while work runs, checkpoint current phase, issue/task/attempt IDs, worker identities, Git heads, resource ownership, latest evidence, pending sync, charge reservations, original elapsed usage, current invocation/batch bounds and exact next actions. Keep secrets out. A fresh context must inspect this checkpoint and live facts before acting. Send the required 20-minute report even when unchanged, and give additional updates for meaningful progress, failures, decisions and completion.

For a failed case, preserve the attempt and diagnose its cause. Continue in finite reviewed batches with actual evidence, a checkpoint and a concrete reason; the lifted overall ceilings do not permit an unbounded individual process. A new hypothesis needs source evidence, while a supported unchanged-case retry does not require a code change or another user approval. Never edit answers, graders, journals, frozen criteria, results or source-skill managed counters to simulate a reset. Report a still-incompatible invoked guard for a separately scoped transition; do not bypass it. Failed or missing acceptance remains incomplete.

## Technical coverage references

These module and connection contracts remain required. Use them to select and verify the active phase items; they do not permit out-of-order delivery. Cross-module dependencies retain their design responsibilities without duplicating progress.

### Module responsibilities

| Module | Working result | Dependencies and existing work |
| --- | --- | --- |
| M1 — Task core and application interface | Scoped task records, lifecycle decisions, dependencies, capacity and durable operations through one application API and its CLI/MCP boundary. | T05–T06, T12, T15–T16, the scoped read portion of T17, and T33 writing-approval enforcement. External observations use declared contract fixtures during independent verification. |
| M2 — Linear synchronization | Authorized issue intake and two-way updates with confirmed readback, conflict handling and scoped events. | M1; T10–T11, T30 and T33’s actual writing path. Connect through LINK-01 before calling the M1–M2 combination working. |
| M3 — Isolated worker execution and lead reporting | The accepted task system runs one actual assigned worker with owned resources, then returns its verified result to the bound lead and user. | M1–M2; T07–T08, T13–T14, T19–T20 and T31–T32. LINK-02–LINK-04 connect startup, supervision and reporting. Code publication belongs to M4. |
| M4 — Review, commit and merge delivery | Workers commit in their own branches; independent review and one product-owned merge authority deliver verified changes to the configured target. | M1–M3; T09 and the delivery portions of T16/T32. Connect through LINK-05–LINK-07. |
| M5 — Terminal interface | A session-scoped CLI, status line and interactive sidebar display and control the same live task system. | M1–M4; T17–T18, connected through LINK-08. Presentation work is admitted in P07; existing projection contracts may support earlier phase checks. |
| M6 — Installation, maintenance and distribution | One self-contained installation per host supports setup, update, recovery, disable/removal and generic single-authority cutover. | M1–M5; T21–T29 and T34, connected through LINK-09–LINK-10. T25–T26 close whole-product acceptance. Minimal isolated packaging is available when an earlier module needs installed evidence; complete lifecycle acceptance belongs here. |

Module IDs identify architecture and required behavior. They do not define a second delivery order. Complete their applicable work through P00–P10. Security, isolation, persistence, error handling and recovery remain part of each responsibility.

### Module and connection completion requirements

A module is **accepted** only when all six conditions hold:

1. Its boundary, inputs, outputs, errors and responsibilities match the governing requirements. The implementation uses the shared application policy; it cannot introduce a second status or authority engine.
2. Its documented CLI/API can reproduce the promised behavior from a clean, owned fixture using the built candidate. No undocumented Python assembly, manual database edits, handcrafted success receipts or hidden setup substitutes for the supported interface.
3. All assigned mechanical, contract and process checks pass, including invalid input, denied scope, stale state, concurrent requests, interruption and recovery where applicable. Required provider/host/platform checks use the real dependency; synthetic controls remain explicitly limited to module isolation.
4. A separate reviewer accepts the relevant source, test selection and results. Findings that prevent the promised behavior are resolved. Reuse matching accepted review; do not repeat unchanged reviews.
5. The reviewed change is integrated and its documented demonstration works on that integrated candidate. Preserve other accepted modules’ affected checks. Required incoming connections have their own accepted results below; a module tested only in isolation is reported as **module verified; connection pending**.
6. A retained completion record identifies requirements/cases, exact source and package, commands, observed outputs/state, reviewer, failures, recovery/cleanup and remaining scope. Report the usable behavior and its limits. No required failure, blocked case or unrun check within the module’s declared scope is hidden by a percentage.

A connection is **accepted** only when the real producer and consumer run together through the supported installed boundary, preserve identity and authority, and pass its positive, failure and restart checks. Record the actual handoff and resulting authoritative states; two separately passing modules do not prove their connection. Live Linear, actual model execution, lead acknowledgement and terminal interaction require their respective real evidence.

Full-product **Done** requires every module and connection accepted, plus all P00–P10 release gates and the complete Test Plan (TP) exit criteria on the final candidate. Module acceptance does not grant normal-session activation or waive an unverified host, account, platform or live case.

### Definition of done for each module

Each row adds required behavior to the common definition above. The referenced requirements and cases remain authoritative; the table is a delivery checklist, not a replacement test inventory.

| Module | Required demonstration and checks | Requirement and verification sources |
| --- | --- | --- |
| M1 — Task core and application interface | Create/read scoped records, explicitly queue eligible work, explain blockers, enforce every allowed/refused transition and all guarded In Review destinations, and preserve human holds. Verify one canonical claim, atomic capacity, no dependency cycles, terminal/delivered-task fences, exact writing approval and idempotent commands. Restart the store/controller without losing tasks, attempts, pending effects or ownership. Exercise CLI/MCP against the same API. Fixtures may supply external evidence but may not count as real issue creation, worker execution or delivery. | R03/R04/R06/R07/R09/R11/R13/R14/R15/R20/R27/R30/R38; P02/P06; transition/cause matrix, admission/storage cases and WR controls in the Test Plan (TP). |
| M2 — Linear synchronization | In live owned TST fixtures, create an approved Backlog issue and adopt an eligible existing issue without duplicate creation; queue it explicitly. Show authorized local changes confirmed remotely and remote changes applied locally without waiting for a periodic poll. Verify human assignee and dynamic connected-agent delegation, two-user/session isolation, holds, reassignments, dependency updates, partial task completion and synchronization rules for each required status cause. Test future delivery-producer inputs as declared contract fixtures; actual code-stage acceptance remains pending LINK-06 until M4 supplies real evidence. Verify signature/replay/order/echo protection, pagination, rate limits, rejected and uncertain writes, durable restart and scoped cleanup. Actual writing approval and human-decision comments/labels follow their contracts. | R02/R04/R09/R10/R12/R13/R23/R30/R37/R38; P04; C09/C17–C24/C39, live TST inventory, RT01–RT03, WR01–WR03 and B19–B20. Only verified producers may authorize actual delivery-stage changes. |
| M3 — Isolated worker execution and lead reporting | Through the installed host integration, start an eligible task with exactly one actual worker and its own branch/worktree and declared database. Enforce task/remote assignment confirmation before execution; no In Progress claim without the required actual startup. Observe progress, failure and exit; distinguish stopped from unknown. Demonstrate hold/cancel, quota/account failure, restart and owned cleanup without a second attempt or foreign effects. Complete the non-code request/result cycle using genuine independent review, confirmed CLI/Linear completion and observed lead delivery, AI acknowledgement and user report. Verify both hosts and each required model/high-effort profile for this module’s applicable cases. | R01/R03/R05/R07/R14/R16/R19/R20/R21/R22/R26/R27/R31/R36; P03/P05/P08; WF-dispatch/execution/recovery/task-loop, B01/B02/B06/B09/B11/B12/B18 and RT04. Full profile acceptance additionally includes the remaining modules. |
| M4 — Review, commit and merge delivery | Deliver an assigned code change from its worker-owned worktree to the trusted configured target through the new merge interface. Independently review the exact candidate; verify enabled commit guards, candidate tree/attribution, current-target checks and confirmed publication. Prove concurrent contenders, moving target, conflicting/duplicate requests, valid and invalid check reuse, resource exhaustion/reacquisition, process death, lost replies and interrupted cleanup. Keep resources until their last possible consumer; reconcile unknown publication before retry. Demonstrate partial issue work and code delivery stages without false completion. Measure small-change, broad-impact, cold/warm and concurrent workloads with per-stage timings and frozen budgets; investigate CG-3589/CG-3596 and retained scenario requirements. | R08–R10/R13/R14/R30–R32; P03 merge/P04/P06; C02–C05/C10–C14/C19–C20/C37–C38, B03–B07, and the reconciled merge Test Specification (TS). No retired shell entrypoint or application script is shipped. |
| M5 — Terminal interface | In a real PTY with the installed product, show only the current session’s authorized tasks, grouped with counts and task activity icons. Verify selection/details, five-line description preview and Show more, activity details, sidebar Hide/Show, whole-sidebar scrolling, keyboard/mouse use, resize/focus, accessible color/ASCII behavior and preserved host-native space. Render pending/conflicting sync truthfully. Controls call guarded application operations; disconnect/reconnect restores the scoped view without lost or duplicate actions. | R04/R17/R24/R27/R36; P07; C25–C30/C36/C39, B08/B09/B13/B16 and the focused terminal specification. Screenshots support actual interaction evidence. |
| M6 — Installation, maintenance and distribution | Install built, pinned packages in empty and unrelated projects for both hosts; verify discovery, configuration scope, credentials, skills, commands and doctor diagnostics without source-checkout assembly. Exercise update, disable, uninstall, export/recovery and interrupted upgrade with validated before-images and no data loss or foreign settings overwrite. Rehearse generic removal of superseded controls and one-authority cutover. Demonstrate private incident capture, deduplication/redaction, a retained failing regression, authorized bounded repair, verified update/reload and safe rollback with recurrence tracking. Verify repository layout, Linux coverage and final combined/endurance evidence. | R18/R21–R29/R33–R35; P08–P10; C31–C34, B14/B15/B17, MA01–MA03 and WF-installation/cutover/removal/maintenance. A later target-specific audit and `docs/migrate.md` remain separately requested work. |

### Definition of done for connections

Test the handoffs below as each receiving module becomes ready. Do not postpone all integration until M6. A shared producer/consumer pair may carry several distinct handoffs; prove each one. Every connection also requires rejection of wrong scope/stale authority, a failed or lost handoff, and restart without a duplicate effect or invented completion where that effect exists. Independent control cases can isolate a boundary; they cannot pass an actual-host/provider row.

| Connection | Handoff and accepted result | Required failure/recovery checks and traceability |
| --- | --- | --- |
| LINK-01 — M1 ↔ M2: task state and Linear | An approved Core operation persists outbound intent; the scoped issue changes and readback confirms it. An authenticated Linear event is durably received, passed through Core policy and reflected in the correct local task. Match task/issue, principal, session, operation and revision throughout. | Lost create/write replies, duplicate/reordered events, illegal remote transitions, concurrent human holds and reassignment, revoked access and two-user sentinels. Restart resumes pending sync without blind recreation or echo loops. R04/R10/R12/R23/R37; WF-sync/realtime-sync, RT01–RT03 and live TST cases. |
| LINK-02 — M1/M2 → M3: admission and startup | Explicit Todo admission, dependencies, current scope, finite capacity and owned resource readiness produce one actual registered CLI worker; confirmed In Progress and startup precede assigned execution. | Omit or reorder every startup step; interrupt setup/start/confirmation; revoke authority; race two sessions. No orphaned Running claim, competing worker or released uncertain capacity. R03/R05–R07/R11/R14/R27/R31; WF-dispatch and omission/order cases. |
| LINK-03 — M3 → M1/M2: worker outcome | The exact attempt’s observed output, verification and independent review reach Core. An authorized non-code completion produces confirmed Linear and CLI results; a failed/partial/unknown attempt remains accurately represented. | Worker text without evidence, timeout, missing review, repeated/lost result, late result after a hold and supervisor restart. A successful exit alone cannot mark the issue Done. R09/R14/R22/R30/R36; WF-execution/task-loop, C07/C08 and B06/B18. |
| LINK-04 — M1/M2/M3 → bound lead: result reporting | A durable scoped event is delivered into the actual bound host; the lead processes that exact event and reports its verified result to the user. Retain distinct remote-confirmed, CLI-displayed, host-delivered, AI-acknowledged and user-reported observations. | Disconnect, idle/unsupported delivery, wrong session, duplicate notification, acknowledgement loss and restart. Pending delivery remains pending. Demonstrate this on both hosts; reading logs or advancing a cursor cannot pass. R16/R36; RT04, WF-task-loop and B18. |
| LINK-05 — M3 ↔ M4: code, review and resource ownership | The settled worker’s exact changes become an attributed commit in its own branch/worktree; independent review and check evidence bind that candidate. The merge authority receives only the authorized candidate and resources. | Wrong file/tree, modified index, hook failure/descendants, changed scope, branch reuse for another task, review of stale source and interrupted handoff. Preserve partial work and keep resources while publication or checks may still need them. R05/R08/R14/R19/R31/R32; worker-commit and resource cases, C06/C12/C15/C35. |
| LINK-06 — M4 → M1/M2: publication and delivery status | Confirmed target ancestry/content and completion of all required work permit In Dev. Verified subsequent delivery evidence advances code through In Staging to Done; partial merges do not complete the issue. A non-code exception does not bypass code delivery. | Target movement, unknown/lost publication response, repeated receipt, incomplete work units, wrong environment and provider failure after merge. Reconcile actual Git before retry; retain a proven merge while sync/cleanup remains pending. R08–R10/R13/R30/R32; WF-delivery, C02–C05/C19–C20 and B03–B07. |
| LINK-07 — M1/M3/M4 ↔ resources: stop and recovery | Hold, cancellation, process settlement and confirmed delivery trigger only the eligible checkpoint/release operation. After restart, inspect Git, worker, database and pending operations before choosing the next step. | Crash at each physical-effect boundary, advancing target invalidating checks, database needed again, unknown process ownership and repeated cleanup. Retain/reacquire the correct resource and preserve foreign sentinels; never signal or delete a guessed owner. R14/R19/R31; WF-hold-resume/recovery, C14–C16 and resource/merge fault cases. |
| LINK-08 — M1–M4 ↔ M5: display and controls | Real task, sync, worker and merge events update the scoped terminal view. Each user action invokes the same guarded Core operation and renders the actual confirmed, pending or refused result. | Delayed/out-of-order events, view reconnect, stale action, other-user/session data, duplicate clicks and lost replies. Verify real PTY interaction and replay without extra mutations; display activity never changes workflow policy. R04/R15/R17/R27/R36; terminal cases and RT04 where lead reporting is involved. |
| LINK-09 — M6 ↔ M1–M5: install, update and cutover | A clean installed package composes the accepted modules through supported configuration and commands. Export/import and validated migration preserve tasks, decisions, pending sync, attempts and resource ownership; one authority serves the same working workflows after activation. | Invalid backup/replacement, interrupted migration, active merge/worker, stale client/writer, changed settings and rollback after new writes. Never reactivate dual writers or discard new facts. R18/R24/R25/R28/R33/R34; C31–C33/B14 and WF-installation/cutover/removal. |
| LINK-10 — M1–M6 → maintenance → verified update | An actual owned anomaly reaches private incident capture with its relevant task/attempt/version. A reproduced regression leads to bounded authorized repair and review, a verified release, safe update/reload and an observable recurrence check. | Duplicate/sensitive reports, missing repair authority, bad update, interrupted reload and recurrence. No private upload, permission expansion, unverified activation or loss of live work. R18/R20/R25/R27/R31/R35; MA01–MA03 and B17. |

For each connection, retain a runnable procedure, exact producer/consumer versions, fixture ownership, expected and observed before/after states, failure/restart results and independent review. The lead accepts the connection only after inspecting this evidence. When a relevant interface changes, rerun affected connection checks and the accumulated demonstration. Reuse unaffected evidence with explicit source binding; run the final combined suite on the release candidate.

## Requirement coverage

Implementation ownership remains grouped below; the Test Plan (TP) and versioned `tests/acceptance/cases.json` own detailed verification/evidence requirements. The work graph in `config/work-items.json` retains all phase/task IDs and dependencies. Component observations do not silently complete those exit gates.

| Requirements | Implementation area |
| --- | --- |
| R01–R03, R11 | Intake, execution and host adapters |
| R04, R12, R23 | Scope policy and Linear adapter |
| R05–R07, R19, R31 | Resources and scheduling |
| R08–R10, R13, R30, R32 | Standalone merge domains, configured project interfaces and synchronization |
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
4. Execute the validated inventory-bound removal/edit transaction: remove wholly superseded files and overlapping sections from mixed files, install prepared product artifacts, then verify callers, scope and the new merge contract. Restore no retired entry point, wrapper or alias. Interruption must resume safely or restore validated owned files while admission remains fenced.
5. Activate one new authority only after replacement validation. The prepared configuration uses an atomic activation marker. New client startup checks the epoch and rejects old task-management mutation paths. Confirm old-session rejection, installed routing and synchronization before admitting normal work. Retain the recovery snapshot and audit record.

These are implementation/delivery steps. The Spec Driven Development document (SDD) owns the observable cutover guarantees; the future `migrate.md` How-to Guide (HTG) owns exact tested operator commands once the migration script exists. No dual writer is an acceptable intermediate state.

### Failure, rollback and uninstall

A failure before activation leaves admission stopped and the old writer set fenced until an explicit recovery path verifies restoration. A failure after new writes requires reconciliation/export of those writes before considering restoration. Never blindly restore an old database snapshot, re-enable old watchers, or run two engines to catch up. A safe stopped system is preferable to contradictory ownership, but independent implementation/test work may continue elsewhere.

Disable/removal stops admission, completes existing merges, checkpoints other work and retains authoritative history. Restore only configuration still owned by this installation; concurrent edits must be preserved. Remove the integration from the selected scope and test that a remaining scope is reported accurately. Uninstall is not authorization to erase unfinished worktrees, databases or history. Purge is a separate explicit action.

### Delivery contents

Standalone delivery includes the Git commit/pushed branch, exact package hashes/versions, locked dependencies, MIT license, self-contained host packages, reproducible build instructions, verified install/update/disable/removal and recovery commands, necessary product-owned guidance, generated references, scenario traceability and state export/import tools. The later requested AI Index audit supplies its separate target-specific change report and tested `docs/migrate.md`; it is not bundled application runtime.

Keep full private run traces and credentials outside the public repository. Commit test code and safe fixtures; publish only redacted summaries and distributable artifacts. Scan the staged tree and final artifacts for secrets, personal names in Markdown, copied user paths, fixture credentials and private source snapshots before an authorized push. Do not push raw application snapshots or local authentication/configuration files.

## Risks and open decisions

The recorded user decisions define standalone behavior and authority. The replacement merge public interface and any necessary guide path still need their own implemented contract; do not invent compatibility names to fill that gap. Present any additional methodology/scope choice for user approval before adopting it. Both hosts remain required. macOS is primary; real Linux-container/CLI checks do not establish native Linux services or desktop integration.

Use the Test Plan (TP) entry criteria for identity, host, resource, cost and receiver prerequisites. The Test Status Report (TSR) owns current evidence and blockers. Two-user acceptance requires two distinct verified Linear identities and live isolation checks; identity preflight alone is insufficient. Continue independent active-phase work while a dependent facility is unavailable, with every required unverified cell visible; do not skip to a later phase.

Lifted total ceilings do not establish completion. At a failed guard or interrupted batch, preserve code, exact evidence, remaining failures and recovery state. Continue independent authorized active-phase work where safe and report incomplete integration readiness until every required gate passes; never reduce scope or erase a failed attempt.

## Deferred Engineering Lead organization

Start this work only after the entire currently approved build is implemented and verified, including its required modules, connections, live Linear cases, host/model profiles, Linux checks and final combined acceptance. This is post-completion work; it must not delay or replace any existing delivery requirement.

| Part | Responsibility |
| --- | --- |
| **Policy** | Defines authority, approval boundaries and obligations. |
| **Engineering Lead** | Interprets requests, plans work, delegates, resolves ambiguity and reports results. |
| **Coordination skill** | Gives the lead a repeatable workflow for those responsibilities. |
| **Scripts and services** | Enforce permissions, transitions, isolation and execution order. |

Detailed design, agent/skill authoring and installation for this organization are deferred until that completion condition is met. The coordination skill and lead-reporting requirements already included in the approved build remain part of the current delivery.
