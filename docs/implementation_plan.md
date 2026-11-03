# AI task manager implementation plan

Status: Approved Implementation Plan (IP); implementation and acceptance are tracked separately in the Test Status Report (TSR), [test_status_report.md](test_status_report.md).
Sources: [Policy (POL)](policy.md), [Spec Driven Development document (SDD)](spec_driven_development.md), [High Level Document (HLD)](high_level_document.md), and `config/execution-policy.json`.

## Goal and specification

Build an installable task coordinator for Claude Code and Codex CLI. Deliver the approved behavior, self-contained packages and evidence that every required acceptance cell passes. A written implementation or a passing component suite alone does not satisfy the goal.

The Policy (POL) owns authority and roles; the Spec Driven Development document (SDD) owns lifecycle, merge and failure/receipt rules. The High Level Document (HLD) owns architecture. The Test Plan (TP), [test_plan.md](test_plan.md), owns methods and exit criteria. This plan owns delivery order and recovery preparation. Preserve requirement/case identities and historical results; version changed expected behavior from approved product principles instead of freezing legacy procedures as requirements.

The user lifted the build's total spending, elapsed-time and retry ceilings. `config/execution-policy.json` records this authorization, finite execution controls, publication boundaries, MIT holder and required host/model profiles. Preserve the original run, accounting and every attempt through restarts; normal-session activation, public release and production changes retain their separate review boundaries.

### Approved scope additions

The user directions of 2 November 2026 supersede the earlier compatibility-only approach. Build the standalone system through its own interfaces and focused domains. The old merge shell entry point and its wrappers/aliases are retired. Update governing contracts and versioned acceptance expectations before dependent implementation; retain all earlier failures and source-bound results. These are required changes, not completed features.

| Work | Required result and verification |
| --- | --- |
| Full merge refactor | Separate orchestration, Git publication, checks/cache, worktree/database lifecycle and reporting into focused local domains. Define and verify the new product merge API/CLI contract. Do not ship `merge-to-dev.sh`, a wrapper or an alias for it. Critically map source scenarios to retained outcomes, corrected behavior or retirement; preserve useful safeguards, owned commits and truthful results. Verify new behavior in independently authored disposable projects. Separate network services need an actual lifecycle or deployment reason. |
| History and performance audit | Start with P-CG-64, Merge Queue Reliability, then inspect relevant CG tasks across statuses, including descriptions, full comments/replies and linked issues. Include comment-only relevance. Distinguish capture from semantic review, reported claims from verified source, and actual reproduction from inference. Trace commit hooks, checks/tests, queue waits, rebases, database setup/cleanup, redundant work and fault attribution. Record separate timings for waiting, checks, database setup, commit and publication. Investigate the reported hour-long path for isolated 5–10-line changes; distinguish measured causes from issue reports and source inference. Target a few minutes for representative low-impact changes on the recorded environment, with benchmark cases and budgets frozen before tuning. Use impact-based checks and provenance-bound reuse; line count alone cannot waive checks. Verify broad-impact, cold/warm and concurrent cases as well. Cover dev advancing during a wait, invalidated check receipts and a database released before its last possible consumer. |
| Complete migration cleanup | The future How-to Guide (HTG), `migrate.md`, must drive a dry-run manifest for superseded instructions, Linear/agent scripts, hooks/jobs, tests, entry points and local task stores. Import required state, fence old writers, verify all callers use replacements, remove only matched obsolete artifacts, then install/activate one authority. Retire obsolete unfinished Linear issues through supported terminal states with reasons/replacement links; retain completed/delivery history and unrelated work. Test interruption and recovery before application use. |
| Decision requests | Add `Waiting for Decision` and a real author mention only when a human choice is required. Use the current Technical Writer Decision Record (DR) template: context, options, recommendation and rationale. If the creator is an agent, target the assigned human. Routine notices do not qualify. Scripts must track requests, deduplicate comments, preserve unrelated labels, handle partial remote outcomes and clear the managed label only when no decision remains unresolved. |
| Dynamic agent delegation | Keep the human Linear principal as assignee and resolve the agent delegate from the verified workspace/session connection. Use stable IDs rather than a hardcoded name; require setup selection when identity is ambiguous. Distinguish creator, human assignee, integration actor, Linear delegate and actual local worker. Revalidate permissions/delegation and test revoked access, reassignment, account changes and competing sessions. Delegation metadata does not prove an agent process or Agent Session exists. |
| Testing authority | The approved plan already authorizes scoped tests and necessary retries. Workers review their code/resources, freeze criteria and run finite measured local batches without repeatedly asking the user. Lead source/result review precedes integration. New permissions, credentials, external resource classes and normal activation retain their existing boundaries. |

### Project independence and final migration

Binding user direction, clarified on 2 November 2026: build and verify the product so it installs and works in an empty project. AI Index supplies read-only scenario evidence, regression lessons and mistakes to assess critically. Its scripts, instructions, architecture and command surfaces do not define the new methodology. The standalone product owns its merge interface; the retired shell entry point is not a compatibility obligation.

Treat AI Index’s existing worktree, database, agent and Linear workflow as potentially defective. A flow working there does not establish that its design, sequencing or policy is correct. Define the standalone workflow from the approved product principles and critically decide which scenarios or safeguards to retain, correct or reject. Regression tests must verify those intended outcomes and failure/recovery cases, rather than freeze legacy mistakes as required behavior.

The product must not depend on AI Index, its checkout, workflow scripts or translations of its old check catalog. No AI Index script may live anywhere in the product repository, including ignored caches, worktrees, examples or regression fixtures. Inspect originals directly, read-only. Remove existing product-side copies through a reviewed ownership/provenance manifest. Keep any required immutable historical source evidence outside the product tree with its hashes and relocation record; create no symlink or import bridge. Author product fixtures independently from intended scenarios.

After the product is complete, the user will request a fresh audit of the then-current AI Index. Inventory all existing files and Markdown instructions, trace relevant callers, and report what the migration will remove, edit, replace or retain, with unresolved items explicit. Delete an entire file when its responsibility is fully superseded; remove only overlapping sections from mixed files and preserve unrelated behavior. Obsolete instructions are removed. Author new product-owned integration guidance only where needed; no old task-management filename or path is a required survivor.

The final migration report must explain the reason and replacement owner for each change, what the script handles, and what the user must change in relevant Markdown files. Prepare tested commands and recovery in `docs/migrate.md`. Fence old writers and remove superseded responsibilities before installing and activating one new authority. No overlapping task, Linear, agent, worktree or merge control system may remain active.

The current AI Index checkout remains read-only throughout this build. Final audit, removal and integration are later user-requested work, not permission to edit it now. Present any further audit-driven change to the agreed methodology or scope for user approval before incorporating it into maintained plans. These binding directions supersede conflicting preservation wording below; they do not approve unreviewed audit proposals or claim completed implementation.

## Current context and approach

Earlier source audits recorded checkout/upstream pins and incomplete comparison limits. Their observations remain historical in the source inventory and external evidence; they are not current checkout claims or standalone build prerequisites. Reinspect the then-current original source read-only before any later target integration. Preserve prior source/version records without copying scripts into this repository or changing the original checkout or refs.

The historical audit covers Linear status/synchronization, issue intake, agent admission, worktrees/databases, merge and instructions. Use those findings to identify scenarios and faults. Existing source and its test harness are evidence to inspect, not product runtime, a required schema or an implementation to reuse.

Retain historical untested paths, including connector writes and killed-session observations, as explicit coverage gaps. Build independent product cases for the intended outcomes. Test and clean only verified run-owned TST resources after verdict capture; never run team-wide cleanup or adopt an old cleanup script as a product procedure.

Use the product architecture and independently authored fixtures. Do not import, copy or invoke the old synchronizer or other AI Index scripts to implement product behavior. Skill Builder produces independently installable skill repositories outside this checkout; assemble verified pinned artifacts into the product. Keep both source skills unchanged.

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
| Product-owned resource and Git interfaces | Implement and verify standalone worktree, database, check and merge capabilities with owned fixtures and trusted configuration. Original application scripts may inform scenario analysis but are not product dependencies or executable test mechanisms. |
| Technical Writer and Skill Builder | Document quality and skill production/evaluation. Use their installed environments, typed runners and actual version hashes; do not guess dependencies or claim fresh behavior from replay. |

The table describes checks to perform, not current availability. `config/research-sources.json` retains preparation-time versions and observations; consult the Test Status Report (TSR) for later evidence. Inspect installed help/current official contracts before executing version-sensitive commands. Authentication alone does not prove model execution, and a local fixture does not prove live acceptance.

Use the approved `claude_roni` profile: resolve its configuration under `$HOME` and pass `CLAUDE_CONFIG_DIR` explicitly to the unmodified `claude` executable. The original `claude` account is unavailable; refuse fallback. The Test Plan (TP) owns credential handling, fixture scope and required distinct-user checks.

## Work sequence

The machine-readable dependency graph is `config/work-items.json`. It contains 11 phase gates and 34 planned work items, including T34, the repository responsibility/layout audit and packaging prerequisite. Split a work item further when necessary, preserving its acceptance and dependencies. Every implementation task gets its own tracked assignment and worktree; overlapping owned files require coordination or serialization. A phase is complete only when its output and evidence pass review.

Use short vertical slices: agree the relevant contract/checks, implement a working user flow, verify it and integrate it promptly. Prioritize one real task through Linear, one CLI worker and confirmed result reporting; then extend to code delivery and remaining profiles/failure paths. Run independent slices in parallel with one tracked assignment and isolated resources per worker. Phase gates define release evidence, not a waterfall delivery order. Update affected documents, reuse matching accepted evidence and retain failures. All final live, host/model and whole-candidate checks remain required.

### Delivery rules

The user approved these working rules on 3 November 2026 after repeated slow delivery. Apply them to assignments, execution and every 20-minute report. They govern delivery method; they do not change product requirements or acceptance criteria.

| Change | Observable rule |
| --- | --- |
| Finish one complete task cycle first | Prioritize an existing owned TST task through queue, actual installed CLI worker, independent review, result reporting and confirmed CLI/Linear updates. Component passes cannot substitute for this complete cycle. Extend the working path to code/merge, remaining profiles and failure cases afterward. |
| Give one worker ownership of the current blocker | Assign the exact reproduced blocker to one tracked worker, with its own worktree and disposable database. That worker runs already approved affected checks in finite batches without another permission queue. Preserve authorization boundaries and original attempt deadlines. |
| Review complete changes independently | Review the delivered change before integration. Reuse accepted source-bound evidence; repeat review only for changed relevant source or evidence, an unresolved finding, or a required acceptance check. Do not create review steps for unchanged successful results. |
| Keep independent work parallel | Fill available slots with eligible independent implementation or review work. Respect measured host capacity and owned file/resource boundaries. Never describe queued or completed workers as active. Finish the approved ancillary work, then admit AI Task Manager work only. |
| Measure delivered behavior and waiting time | Report the user workflow advanced, its exact remaining blocker and owner, and observed implementation, test and waiting time. Distinguish unknown durations from measured ones. Test counts and evidence files support delivery claims; they are not delivered behavior. |
| Change approach when work stalls | Use the existing progress guard before additional preparation, review or tests. Two unchanged failures require diagnosis, a changed implementation/integration approach or an eligible dependency; do not rename the same retry. Identify every worker waiting on the lead in the next report and resolve the wait before admitting more preparation. |

Before each new assignment, review or test batch, check when the last status table was actually sent. If 20 minutes have elapsed, settle urgent owned process obligations, send the report, and record its actual delivery time before continuing. Confirm one worker's acknowledgment and task state, then send that worker START immediately; do not batch confirmations or leave an admitted worker waiting for dispatch.

Every report includes P00 through P10 and the merge subrow, with Last round and Current as `X% – x/y`, Done since last report, and Next / blockers. Preserve fixed milestone denominators and show component deliveries separately. Keep actual live Linear and required host/model acceptance visible, and report ancillary completion separately from product progress. State None when no delivery finished. Include any wait on the lead, its owner and the next action; a scheduled wake or completed check alone is not progress.

### P00 — Bootstrap and verify prerequisites

Read the approved product policy and maintained sources. Resolve `PRODUCT_REPO` in the separate build workspace and inspect existing content before acting. An empty-project build/install must not require the AI Index checkout or its instructions. Read-only historical analysis may separately discover `SOURCE_REPO`; that optional evidence source grants no execution or modification authority. Clone only the authorized product repository using structured arguments and preserve unrelated work.

Use the recorded private build ledger and preserve its run ID, UTC start, accounting, attempts, source/requirement hashes and policy amendments. Build-only operational records stay in their declared ignored storage and are excluded from installed artifacts; they are distinct from installed product state. Do not move or reset the existing ledger as part of source-copy cleanup. Continue finite measured reservations and per-invocation bounds.

The bootstrap may use the existing host's task/delegation tools and a deterministic build ledger until the new engine passes its gates. This is development scaffolding, not a claim that the new product is already enforcing assignments. Register development work in the run's TST project through an audited isolated path. Workers still receive one task and separate worktrees and fixture databases. Keep bootstrap scheduling separate from product policy; replace scaffolding only after its replacement is tested.

Prove authentication, file access, model access, network, Docker, disk/memory headroom, worktree creation and a tiny disposable database operation. Run dependency probes in the same environment and permissions as the eventual cases. Resolve host approvals and hook trust once in the intended configuration; do not suppress host safeguards to make tests pass. Missing capabilities block only dependent work and remain explicit in the final result.

P00 uses the claude_roni profile for Claude authentication and authorized model-access probes. Resolve its configuration path under the user home and pass CLAUDE_CONFIG_DIR explicitly to the unmodified claude executable in automated processes; shell functions need not exist there. The original default account is disabled. Both-host acceptance still requires actual tool-enabled evidence.

Gate: trustworthy environment inventory, nonempty authorization/limit record, working policy, cost and capacity admission with per-invocation deadlines and a durable checkpoint. Verify the primary test identity before live TST writes. A missing secondary Linear identity blocks two-user cases only; missing Claude access blocks Claude execution tests only. Keep independent core/mechanical and available Codex work eligible. Do not start an unmetered paid batch.

### P01 — Freeze contracts and baseline

Inspect previous workflow rules and callers only to extract critically reviewed scenarios and prepare the later target-specific removal inventory. Include task/agent instructions, Linear writers, resources, hooks, timers, watchers, background processes, pipeline callers, connectors and nested settings. Trace actual consumers and dynamic configuration. Treat old rules and implementations as evidence; the approved product requirements determine authority.

For the later migration, map each relevant rule/caller to its source/hash, intended outcome, retain/correct/retire decision, new owner, state to import, fencing/removal action and verification. Preserve a command surface only when the user still requires it; the old merge shell entry point is retired. Resolve every intersecting row before target activation, with wholly superseded files removed and only overlapping sections removed from mixed files.

Implement T34 in its normal dependency order before packaging, using the responsibility map in the High Level Document (HLD) and `config/repository-layout.json`. Move each declared source and its consumers together, preserving before-image hashes, historical paths, command names and arguments. Keep prose, editable/rendered diagrams and screenshots under `docs/`; configuration, tools, acceptance oracles, published indexes and prototypes use their declared homes. Run `python3 tools/repository_layout.py`, the relocated handoff checker and focused ownership/consumer tests. Stop any model or helper bound to the old paths before integrating the relocation; the lead updates current private consumers at that boundary. Preserve the coherent commit and verified before-images for collision-checked recovery. Installed-package and whole-product acceptance remain later gates; folder separation does not require network services.

Expand acceptance from the approved standalone policy, its complete status-pair/cause rules and critically reviewed scenario inventory. Freeze actual fixtures, negative controls, fault seams and expected outcomes before tuning. Preserve earlier case IDs/results as history and version intentional corrections explicitly. Do not use a legacy algorithm or its output as the correctness oracle.

Build and run independently authored product fixtures and test procedures in owned disposable projects. Retain historical baseline failures and unrun source scenarios without copying or executing the old harness. Gate: reviewed standalone contracts, reproducible fixtures, explicit source-derived coverage and a complete implementation responsibility map. Later target-specific inventory closure remains a separate cutover obligation.

### P02 — Implement core records and admission

Implement pure domain rules and one application API. Separate task intent, execution attempt, worker identity, resource owner, delivery evidence, human decision and synchronization operation. Keep domain imports independent of providers and presentation. Enforce boundaries with executable import checks.

Implement schema-versioned SQLite storage, optimistic versions, unique claims, atomic capacity reservations, append-only audit events and durable outbound intents. External process/API actions do not participate in the SQLite transaction: reserve, perform, confirm and compensate/reconcile explicitly. A schema migration must preserve a before-image and have a tested recovery path.

Gate: all pure policy, scope, transition-pair, transaction and architecture tests pass on exact artifacts. Invalid or premature actions change no unauthorized state.

Before merge integration, freeze the new standalone merge contract and critically reviewed retained/corrected scenario expectations. The earlier R08 revision 3 keep-current-unless-incompatible restriction is superseded. Audit relevant source/history as evidence, preserve failures and verify concurrency/recovery in independent fixtures. Neither the old script, its byte layout nor a compatibility alias is required.

### P03 — Implement workers, resources and the first complete path

Prepare owned worktrees and required databases with durable identifiers, confined paths and compensation for partial setup. Use separate disposable SQLite fixtures for product-development tasks. Exercise other configured database providers with independently authored resources; the old application stack/scripts are not a standalone prerequisite. Control metadata is never a worker application database. Verify target-specific resource integration only in the later authorized target audit/cutover.

Launch and supervise actual worker processes, recording PID plus process-start identity, host session/thread ID and attempt capability. Distinguish startup, liveness, progress and exit. A heartbeat is not progress; a worker message is not proof of success. The startup handshake must prevent task execution before the task and remote assignment are confirmed.

Prove one real task through registration, an observed worker, isolated owned resources, a committed fixture change, the new product merge engine and checked cleanup. Reconcile final Git and Linear facts. Add refusal and crash cases at every effect boundary. Gate: the complete standalone one-worker path and recovery work before increasing concurrency.

#### Deterministic run and resume controller

Implement R15 revision 2 as a script-owned loop over saved Core, provider, runtime and operation facts. Keep workflow intent/claim history separate from Core task status. Start with a bounded non-code path using existing guarded worker, resource, verification and synchronization ports. Missing integration or genuine semantic acceptance produces a named wait; it cannot produce success.

Freeze omission, wrong-order/file/scope, concurrent-claim, human-change, lost-reply and lineage cases before implementation. Verify a real owned local worker and resources, durable restart without duplicate effects, and preserved partial work. Do not import the private report evaluator or its answers. Then connect public CLI/host invocation and code delivery through their owners, and run the actual-host omission cells. Component tests do not satisfy those later gates.

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

Implement generic installation, state migration and removal through the standalone product interfaces before normal activation. Remove superseded controls instead of wrapping them. Generate transition references from the executable policy and test parity. Keep product rationale and necessary operator guidance readable; no inherited instruction filename is mandatory.

At target cutover, delete obsolete workflow instruction files and remove only overlapping sections from mixed documents. Author concise new product-owned guidance only where actual consumers need it; no old task-management path must survive. Keep essential shared project instructions concise. Claude may use `@AGENTS.md`; verify actual imports and supported Codex discovery separately. Load detailed generated matrices and test history only when needed.

Build self-contained Claude and Codex packages from the same core and pinned skill artifacts. Native plugin formats and tool discovery may differ; test the installed artifacts, not just source directories. Implement install/doctor/update/disable/uninstall and state export/recovery. Resolve ownership and compare current settings before restoring any before-image. Do not store authoritative state in plugin caches or remove another integration's files.

Rehearse generic clean cutover in independently authored repository fixtures with tasks, holds, live workers, merges, pending sync, worktrees/databases and stale clients. Verify representative old-writer fencing without copying AI Index scripts. After standalone product acceptance, the user will request the fresh AI Index audit and target-specific adaptation/removal rehearsal. Neither stage permits dual live authorities.

After the user requests the final target audit, deliver `docs/migrate.md` with exact tested commands and an inventory-bound removal/edit manifest. Validate backups and prepared product artifacts, fence old writers, remove superseded responsibilities, install the new system, then verify one authority before activation. Use the new merge contract, with no retired shell entry point or alias. Test interruption, resumable recovery and rollback. Markdown guides the operator; scripts perform the changes.

Gate: all required records and contracts migrate; old writers are fenced; the new system alone passes installed acceptance; repeated install/remove and interrupted upgrades preserve data.

### P10 — Complete acceptance and prepare delivery

Run the full final suite against the exact candidate artifacts, including live TST, installed host behavior, clean cutover, failure recovery, real terminal interaction, install/remove, second-project portability and Linux-container coverage. Fix failures through bounded, evidence-based iterations, then rerun impacted cases and the required final release checks. Preserve all attempts.

Use remaining time for a measured mixed-workload endurance run with deterministic faults, multiple sessions, dependency chains, varied task sizes and restarts. Record the actual duration, workload, concurrency and invariant checks. Do not claim a 48-hour soak from a shorter test or treat the build's elapsed time as endurance evidence.

Gate: the release checker accepts every required case/profile, source and package hash, migration row and evidence record. Produce the final evidence report, install/remove commands verified against the built version, operator recovery guide, known limits and integration patch. Normal-session activation remains a final review action.

### Autonomous operating procedure

The current build controller has already applied its recorded schema/policy amendment. Preserve the receipt, original ledger, start, charges, attempts and failures; do not repeat a completed migration because an older prompt calls it pending. Any future controller migration needs a fresh private backup, exact state/authorization binding, reconciled launch ownership and supported API review before execution.

Preserve the original build run and UTC start. Record the authorized policy amendment and its receipt without resetting elapsed usage, calls, spending or failures. The amended total ceilings are explicitly absent; keep finite measured capacity, reviewed batches and a monotonic timeout for every supervised invocation. A policy change or expired timer cannot establish that a worker stopped, release uncertain capacity or justify killing a merge blindly.

Choose finite invocation and spending allowances for each reviewed execution batch. Include workers, lead invocations, graders, retries and chargeable services in the same durable accounting. A later batch is an evidence-supported continuation, not a fresh run or a way to erase earlier usage. There is no overall retry-round ceiling under the recorded user authorization.

Use one atomic accounting ledger and finite batch reservations. Before a paid call, reserve a defensible maximum charge from current official pricing and enforced provider/transport limits; reconcile actual usage afterward. Parallel calls cannot reserve the same batch allowance or worker capacity. If a CLI transport cannot establish a bounded charge or usage provenance, use a supported bounded transport or block that paid call. Subscription sign-in alone does not establish unlimited allowance, zero incremental cost or a spending cap. Unpriced tools and new paid infrastructure remain unavailable.

Choose phase batches by dependencies, actual capacity and cost. The build runner schedules bounded model turns and deterministic commands, consumes their structured outcomes, persists a checkpoint, and resumes the next eligible task. A single interactive chat turn does not establish a continuously supervised background service. Persist child process identities, model turn IDs and job handles so loss of the chat does not create duplicate launches. If the host cannot sustain supervised background execution, report that preflight limitation before starting an unattended claim.

Use measured progress and phase dependencies to update forecasts and the eligible work queue. Retain earlier milestone estimates as historical planning evidence; they are not current deadlines or permission to bypass a phase gate. Report delays and remaining work without removing accepted requirements.

At each completed work item and at least every five minutes while work runs, checkpoint current phase, issue/task/attempt IDs, worker identities, Git heads, resource ownership, latest evidence, pending sync, charge reservations, original elapsed usage, current invocation/batch bounds and exact next actions. Keep secrets out. A fresh context must inspect this checkpoint and live facts before acting. Send human-facing updates on meaningful progress, failures, decisions and completion; do not emit unchanged heartbeat noise.

For a failed case, preserve the attempt and diagnose its cause. Continue in finite reviewed batches with actual evidence, a checkpoint and a concrete reason; the lifted overall ceilings do not permit an unbounded individual process. A new hypothesis needs source evidence, while a supported unchanged-case retry does not require a code change or another user approval. Never edit answers, graders, journals, frozen criteria, results or source-skill managed counters to simulate a reset. Report a still-incompatible invoked guard for a separately scoped transition; do not bypass it. Failed or missing acceptance remains incomplete.

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

Use the Test Plan (TP) entry criteria for identity, host, resource, cost and receiver prerequisites. The Test Status Report (TSR) owns current evidence and blockers. The second Linear identity has been supplied; identity preflight still does not prove two-user acceptance. Continue independent work while a dependent facility is unavailable, with every required unverified cell visible.

Lifted total ceilings do not establish completion. At a failed guard or interrupted batch, preserve code, exact evidence, remaining failures and recovery state. Continue independent authorized work where safe and report incomplete integration readiness until every required gate passes; never reduce scope or erase a failed attempt.
