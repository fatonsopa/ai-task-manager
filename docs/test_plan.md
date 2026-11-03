# AI task manager test plan

Status: Approved testing requirements consolidated from R01–R38 and the existing authored inventory; this document records a plan, not execution results.
Basis: [Policy (POL)](policy.md), [Spec Driven Development document (SDD)](spec_driven_development.md), [High Level Document (HLD)](high_level_document.md), `tests/acceptance/cases.json` and the pinned migration/regression inventory.

## Objectives and scope

Establish that authorized work travels from the user's request through Linear, the scoped CLI, an actual worker, verified delivery and observed lead/user reporting without lost writes, unauthorized ownership or invented activity. Assess recovery, distribution and the project adapter as well as pure policy. Keep 38 requirement IDs, 248 authored case IDs and 14 workflows stable; preserve version history for authorized criteria changes.

Required layers are mechanical/domain/storage/contract checks, workflow-process/fault checks, live Linear, fresh installed-host behavior, terminal interaction, installation/removal/cutover, second-project portability and real Linux-container coverage. Both Claude Code and Codex CLI need their own actual installed execution evidence. A model's self-report, replay or local receiver fixture cannot replace it.

No production mutation, live failure injection, team-wide cleanup, public release or normal-session activation is authorized by this plan. Native Linux service/desktop acceptance is not claimed from a container on macOS. Independent work may continue while a dependent facility is blocked; blocked required cells still prevent release.

## Risks and test basis

Prior regressions include dropped writes, stale automation overriding human holds, cross-account/session/task IDs, startup before an actual worker, unknown detectors treated as no process, partial commits advancing entire issues, and carried results proving unrun/new artifacts. The [contract lessons](migration/current-contracts.md) and [located audit](audits/coordination-policy-audit.md) retain sources and targeted oracles. Preserve proven working behavior as well as negative cases.

Each authored workflow includes a valid path, an omitted-step case for every step and adjacent-order violations. Enumerate every status pair/cause and all guarded In Review destinations; add reviewed source-derived cases without deleting the original cases. Missing lookup/adoption/follow-up integration and actual resource/merge/host paths must be tested as required behavior, not declared present because a diagram shows them.

TST-85 freezes 84 existing-issue case families in `tests/existing/frozen/`, preserving the original 68 and every versioned addition. Exercise metadata-first privacy, complete initial-Backlog spans, deleted/renamed states, early activity-log omissions, pagination and revision races, exact scoped Writer approval, independent lead selection, canonical process claims, explicit Todo admission, terminal/delivery resets and restart recovery. Hostile issue instructions remain data. Test enrolled-store rejection with independent stores, copies and replacement at actual transaction boundaries; a pathname-only check is insufficient.

For each admission, queue and startup boundary, prove rejection of discovery-only, stale, borrowed, wrong-purpose and mismatched version/revision receipts. Verify a complete provider reread issues the right operation-bound receipt, and a committed same-key replay creates no new effect after source expiry. Test a canonical delivery observation before both ordinary and atomic short-effect work checks, new local/staged prose authority and linked-follow-up eligibility, without rewriting the cached Task during lookup or blocking genuine observations. Imported prose confirmation and remote Todo settlement must leave an unqueued task unqueued; reserve/start must refuse until explicit guarded enqueue. Assert already-queued membership, nonimported behavior and exact replay remain intact.

EA84 retains the actual copied-connection failure and verifies that the repaired guard refuses before a read-only work-boundary effect. Test normal DELETE operation, actual unsupported WAL/OFF/MEMORY/PERSIST/TRUNCATE modes, read-only/query-only and unknown-mode failures, UUID generation/equality, conditional row matching, write/rollback/release failures, path-change ordering, actual process crashes, unchanged outer domain facts and stable replay. Exercise both human-hold orderings over the same physical store. Record macOS and Linux observations separately; do not infer a descriptor measurement or platform acceptance from a pathname, a successful SQL write or a fixture label.

Use authored loopback HTTP and explicitly synthetic selection observations for mechanical tests. Build and install the wheel in an isolated environment, then run its actual CLI entry point and the real MCP SDK stdio protocol. The test-only bootstrap may inject only the loopback HTTP destination; production configuration retains its fixed provider endpoint and authority checks. Retain wheel/bootstrap/source hashes and failures. These checks cannot substitute for live history semantics, an actual host selection observation, two-user isolation or installed model behavior.

## Approach and resources

### Test levels and sequence

Start with deterministic pure rules and transactional storage: scope, exact writing approval, versions/idempotency, every transition/cause, one claim, finite capacity and durable events. Check import boundaries so core policy depends on neither providers nor presentation. Use state-machine/property tests where sequences expose errors; review tests and fault oracles before executing them.

Then test real process and loopback transport boundaries, followed by independently authored project/worktree/database/merge fixtures, authenticated live TST fixtures, installed hosts and terminal interactions. The Implementation Plan (IP) owns dependencies and delivery phases; phase gates reference these criteria instead of defining another acceptance standard.

### Failure and recovery

Exercise concurrent claims across sessions, global capacity and spend reservations, occupied ports, stale locks, quota pauses, missing credentials, a detached lead, process identity reuse, missing progress and disk/storage failure. Introduce crashes before/after each durable reservation and external action. Recovery inspects actual processes, Git, databases and remote facts before restarting work. Unknown state prevents a competing attempt.

Inject failure before/after durable claims, saved intents, process/resource creation, provider writes/readback, received-event persistence, domain application and each host/AI/user receipt. Distinguish definite rejection from a lost response after a possible effect. A later retry cannot erase the earlier attempt. Keep independent sentinels for foreign tasks/resources and concurrent fixture runs.

TST-144 freezes ER01–ER10 in `tests/workflows/frozen/expired_recovery_v1.json` for R14/R15/R20/R36. The local controls in `tests/workflows/test_expired_recovery.py` cover unchanged history, exact old-key binding, current authority and eligibility, attempt/resource/native/provider refusals, original stopped-owner identity, concurrent successor creation, process death and lost replies, precommit rollback and schema migration. Use actual local files, SQLite transactions and owned processes with explicitly authored authority/provider observations. A replacement journal or missing admission witness must remain a refusal. Run the affected workflow, public-owner, installed-composition and review controls on matching source/package bytes; these checks do not substitute for live provider or native-model acceptance.

### Environments, data and identity

Record exact source/package/case hashes and host/model/effort/OS profiles. Use separate disposable fixture databases for development workers; resource-provider cases need a disposable implementation of their declared resource contract. Prove installation and useful work in an empty project without the original application stack or script catalog. A later target-specific migration rehearsal is separate. Control metadata is separate. Verify resource ownership before idempotent cleanup and retain cleanup failures.

For this authorized build, reuse the user-approved existing `LINEAR_API_KEY` in the application repository’s `.env.local` as the primary credential when `LINEAR_TEST_USER_A_TOKEN` is not explicitly configured. Preparation-time and later development-project probes are recorded separately in the Test Status Report (TSR); repeat identity/scope checks for each actual fixture run. Read the key in memory through an allowlisted parser. Do not copy it into the product repository, handoff or logs, and do not request a replacement primary key. Standalone installation uses explicit credential configuration and must not require that application checkout.

When a second Linear test account is available, run `python3 tools/setup_test_credentials.py --secondary-only` in the user's interactive terminal. It saves `LINEAR_TEST_USER_B_TOKEN` in `$HOME/.config/ai-task-manager/test-credentials.env`, with directory mode 700 and file mode 600, while preserving any explicitly configured primary token. The default helper mode can save two dedicated test tokens if desired; `--init` creates an empty template and preserves an existing file. Use the narrowest available permissions and distinct real users for two-user cases. Do not paste tokens into a chat, issue, command argument, trace or repository.

Before any remote mutation, parse the selected credential files as data with allowlisted keys; never source them as shell code. Check permissions, authenticate the primary viewer, verify the intended workspace, and confirm TST's team ID is `cbe80ecc-8035-407d-b531-63cfe083aafc`. Single-user TST cases may use this verified credential. Before two-user cases, also authenticate the second viewer and verify distinct immutable user IDs in the same intended workspace. Resolve its real workflow-state IDs. A token is not evidence that these checks passed. If an app token is used separately, bind its authority to the validated responsible user; never treat an arbitrary model-supplied assignee as authenticated identity.

Create a run-owned TST development-tracking project, and separate fixture projects for live case attempts. Keep unfinished development work truthful on interruption. Fixture cleanup may close only proven run-owned fixture issues and complete their fixture project after verdict capture; it cannot make an incomplete development task Done. Do not delete Linear issues, change workflow configuration or workspace membership, or change other users' work. Webhook configuration is limited to the separately reviewed run-owned fixture path below; existing unrelated hooks and production teams remain untouched. Use supported project/issue creation, the existing type conventions and explicit relationships.

Use authenticated Linear webhooks as the normal inbound path and an immediate durable outbox as the outbound path. Stream applied events to the scoped CLI and lead notification channel. Reconciliation polling is a recovery mechanism, not the normal real-time path; the old ten-minute timer is not acceptable as real-time sync. [Linear's GraphQL documentation](https://linear.app/developers/graphql) defines the API; [rate-limit guidance](https://linear.app/developers/rate-limiting) governs request pacing. Handle GraphQL errors even when HTTP succeeds, pagination, partial results, revoked credentials and rate/complexity limits. Discover actual limits; old numeric quota comments are not a runtime contract. A publicly reachable HTTPS webhook receiver is required for actual Linear-to-local real-time delivery. Test [signature, freshness and replay checks](https://linear.app/developers/webhooks), deduplication, causal ordering, recovery and session filtering. Local receiver tests do not prove real provider delivery; the live TST gate needs a reachable ingress and the appropriate webhook permission.

For live inbound coverage, preflight the actual identity, canonical TST team, permissions, receiver and cleanup plan. The lead first reviews the receiver command/artifact hash and ownership ledger, then an owned temporary tunnel may start to obtain its dynamic URL. Verify the actual HTTPS URL and route before reviewing and creating the hook. Only the ledger-owned Issue webhook for the canonical TST team is allowed, with `allPublicTeams=false`; no workspace-wide hook, production-team hook, paid hosting or unrelated-hook changes are authorized.

Linear's team filter may deliver unrelated TST activity. Authenticate the routed event, then acknowledge legitimate unrelated deliveries with HTTP 200 and only minimal delivery/digest/reason metadata, without importing or exposing foreign task content. Verify cleanup removes only the run-owned hook and stops only its owned tunnel/receiver; never sweep the team or stop another session's infrastructure.

R08 revision 6 requires standalone merge verification in independently authored disposable projects. The old shell entry point, wrapper and alias are retired. Use the new product interface only after its contract is defined; no replacement public command is invented here. Test foreground completion, one publication authority, trusted configured targets, meaningful check selection, stale/carried evidence, Git ancestry, partial commits, owned resource lifetime and lead-coordinated missing-worker recovery. Old source is external read-only evidence, not an executable harness, required check catalog or a correctness oracle. Preserve earlier failures and map scenarios critically to retained, corrected or retired behavior. No standalone merge acceptance is claimed by this plan. Staging/production delivery events are explicitly isolated fixtures; never deploy the real application to obtain a test pass.

### Fresh behavior evaluation

Freeze the host/model/effort matrix before a broad batch. Use every selected profile in `config/execution-policy.json` under `acceptance.required_profiles`: at least two models on each CLI, with Fable excluded. Run each declared behavior case and all three repetitions of the seven drift-critical cases on each required profile. Keep complete results separate; one passing model cannot replace another required profile, and results from different models or efforts cannot be mixed. Unknown observed model identity remains unknown, never guessed.

Create performing packets with realistic inputs and necessary resources but no expected answers, grading criteria, frozen grader or prior outputs. Execute in fresh contexts through the actual installed skill with tools. Use live TST fixture tasks for task-management cases. Review actual public traces and artifacts with a separate evaluator context and deterministic state oracles. The performing model's self-report cannot grade its own work. Do not claim independent review if the same context performs it.

Retain input, trace, package/case hashes, host/model/effort, duration, measured usage, artifacts and assessment for every attempt. Keep mechanical, workflow-process and behavioral results separate. A timeout is a failed attempted task. Missing observations are not run. Controls, replay and historical traces are useful but do not count as fresh performance.

Gate: every required host/model/effort profile satisfies its complete behavior cells, repetitions and mechanical checks on the release candidate. Missing observations or an unavailable host prevent acceptance.

Test the supported native agent/task tools, shell/SDK/CLI launch paths, direct MCP operations and stale configuration. A general shell is not a universal sandbox: if a required host surface cannot be guarded, report the host incomplete. Verify actual task admission and startup, not a waiting harness. Perform the complete request/result loop with tools and separate observed lead delivery, AI acknowledgement and public report.

Use real PTY evidence for sidebar mouse/keyboard behavior, focus, resize, wrapping, scrolling and host-native space; browser prototype checks and screenshots are supporting evidence only. Preserve `NO_COLOR`, ASCII and accessible labels. The [terminal specification](terminal/spec_driven_development.md) owns interactions.

### Maintenance, migration and distribution

Test private incident capture/deduplication and redaction, reproduction before bounded repair, retained failing regression, compatible verified release, safe checkpoint/update/rollback and recurrence handling. Refuse unapproved private uploads and permission expansion.

Rehearse inventory-complete cutover with active/held tasks, merges, stale sessions/scripts, pending sync and owned resources. C32/C33/B14 must exercise obsolete removal before replacement, invalid backup/replacement refusal and interruption recovery without two live authorities. Test installation scopes, owned-settings restoration, direct native uninstall without assumed callbacks, retained unfinished history and portability to another project.

## Entry and exit criteria

Entry requires the frozen requirement/case/artifact hashes, reviewed tests/oracles, configured authority/finite budgets, actual dependency availability and verified fixture scope. Check the chosen host/profile, loaded guards, identity, HTTPS route/webhook permission, resource readiness and evidence capture before the dependent case. Missing facilities produce blocked cells, never fabricated observations. The distinct second Linear identity gates only live two-user cases.

Acceptance requires all mandatory cells and declared repetitions to pass on the final candidate, zero unresolved critical failures and no required blocked/not-run cells. Current component results are in the Test Status Report (TSR), [test_status_report.md](test_status_report.md); do not sum overlapping component counts as independent acceptance. A time/spend stop cannot waive a gate.

| Requirements | Acceptance evidence |
| --- | --- |
| R01–R03, R11 | Intake/dispatch workflow checks; C01, C07; B01, B06, B09. |
| R04, R12, R23 | C09, C17–C24, C39; B04, B08, B10; real distinct-user readback. |
| R05–R07, R19, R31 | C06, C12, C15, C35; workflow omission/order checks; B02. |
| R08–R10, R13, R30, R32 | C02–C05, C10–C14, C19–C20, C37–C38; B03–B07. |
| R14, R20, R26 | C07–C08, C14–C16, C40; B06–B07, B11–B12. |
| R15–R18, R24, R27 | C25–C30, C36, C39; B08–B09, B13, B16; installed package traces. |
| R21–R22, R25, R29 | C34; every required host/profile cell; B15; immutable report bindings. |
| R28, R33–R34 | C31–C33; cutover/removal workflows; B14; complete inventory parity. |
| R35–R38 | spec_driven_development.md; WF-maintenance/task-loop/realtime-sync/task-writing; RT/MA/WR cases and B17–B20. |

The normative workflow mapping adds WF-maintenance/task-loop/realtime-sync/task-writing, RT01–RT04, MA01–MA03, WR01–WR03 and B17–B20. Exact procedures, expected outcomes, host requirements and status remain in `tests/acceptance/cases.json`; this plan does not create a second case inventory.

## Reporting and follow-up

Every final report identifies requirement/case ID, exact artifact and case hashes, attempt, setup, command or public trace, before/after authoritative state, expected/observed results, status, reviewer, host/model/effort/OS, duration, cost and cleanup result. Record `passed`, `failed`, `blocked` and `not_run` distinctly; distinguish replay from fresh execution. Secret values are never evidence attachments.

Report counts and denominators separately for mechanical, process, live Linear, behavioral, terminal, installation and platform coverage. Do not replace missing evidence with a coverage percentage. A budget/time stop cannot waive a gate.

Keep raw private traces/credentials outside public artifacts; publish only safe fixtures, distributable packages and redacted summaries. The Test Status Report (TSR) owns ongoing observations. A final completion report is due at the release milestone, not created merely to fill a document catalog.

For a failed case, preserve the attempt and diagnose its cause. Continue through finite reviewed batches with a current checkpoint and supporting evidence; the authorized build has no overall retry ceiling. Rerun impacted checks after a fix and the complete required final suite on the exact candidate; repeat unchanged passes only for a new concern or declared repetition. Preserve earlier source-skill guard records and report incompatible guards for a scoped transition instead of resetting managed state. Never change frozen expectations or results to pass. Missing observations are not run; an observed timeout remains a failed attempt.

Use a finite reviewed batch for measured mixed-workload endurance with deterministic faults, multiple sessions, varied tasks and dependency chains. Report actual duration, workload, concurrency and invariant checks; elapsed build time and lifted overall ceilings do not prove an endurance duration. Cleanup only proven owned fixtures after verdict capture and retain unresolved cleanup/recovery state.

### Captured interface references

The preparation catalog records [Claude programmatic execution](https://code.claude.com/docs/en/headless), [Claude hooks](https://code.claude.com/docs/en/hooks), [Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode), [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins). These are captured source references, not a claim of a new documentation or host probe. Verify current installed help and official contracts before version-sensitive execution.
