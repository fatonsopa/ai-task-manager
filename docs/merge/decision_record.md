# AI Index integration boundaries

Status: Draft
Decision MERGE-BOUNDARY: proposed integration design; source findings and independent-work scope reviewed by the lead. Exact implementation and affected fixture gates remain pending.
Scope: AI Index project adapter, MERGE-COMPATIBILITY / TST-84.

The approved coordinator requires one task-state authority, task-owned resources and recovery from uncertain effects. The existing merge route already supplies substantial Git and test safeguards. The missing project adapter is an implementation gap; it does not justify replacing that route.

Three located integration conflicts require focused changes to its dependencies: legacy callbacks can write Linear independently, database sweeps can remove resources outside the copied repository, and a blocking merge has no coordinator authority boundary at its eventual Git effect. Preserve `scripts/merge-to-dev.sh` as the default route and regression baseline. Adapt these boundaries rather than reproduce the merge algorithm.

The governing requirements are R08 revision 3, R09, R14, R28, R31, R32 and R34 in the [requirements registry](../config/requirements.json), together with the [protected merge behavior](../spec_driven_development.md#protected-merge-and-command-behavior). The [Test Specification (TS)](test_specification.md) defines the proposed frozen checks. No merge, application database, provider write or new implementation has run under this assignment.

## Source and evidence boundary

The inspected application commit is `44a91116d73d0a3ac279aa3b064d373000356477`. The lead supplied this refreshed pin, and read-only local Git metadata still names it. The earlier formal baseline `873155ac73f486912e4e9fe6e3daa719388e0665` remains historical evidence. The merge script SHA-256 is `b47d53d4f9861e2a22e1412dd1d9b8c48407bf177c232cb97ff4770acd307022`. Source locations below refer to that commit; reads used Git objects without changing the application checkout or refs.

The reviewed assigned-worktree merge source is `3560ecf3825df74e868b92e108e3c2a79e755433`, integrating root main `76a23ae` at the start of this document phase. This identifies the reviewed worktree source, not a main-branch commit. The original product baseline `b384f10278b0ee6b0bcd72fef161e8327cdf491d` remains historical. Host bridge checkpoints `ede7e819` and `c19cf44` expose an observed integration port; they are separate source evidence, not proof of integrated or live acceptance. Private source/hash records remain outside public distribution. Public documentation paraphrases the contracts rather than copying private implementation.

### Audit coverage and limits

The audit now covers root and nested instructions, role/style files, native task and agent hooks, commands/skills, Git effects, Linear callers, worktree/database helpers, CI/background jobs, stores and credential/resource routes. The captured catalogue has 646 paths and 396 source-linked contract rows. The latest audit phase added 30 focused rows covering 62 paths. These are source observations, not executed behavior or complete-file certificates.

There are still 358 captured paths without a contract row. The prior 417-path semantic worklist remains: 59 paths now have focused rows but still need their other relevant branches reviewed. All 95 inherited static caller rows remain visible. At their exact reference points, 27 are documentation/comment references and 68 are executable, configuration or instruction consumers; that classification does not certify whole files. Unknown installed services, credentials, effective host settings and stale executable consumers remain cutover gaps.

Twenty-eight inventory-data checks pass for source/hash/anchor binding and preservation. Earlier parser, source-anchor and validator failures are retained with corrected successor records. These checks do not establish that any merge, application database, model, provider or cutover case passed. Complete semantic closure is required for each activated route; unrelated unadmitted surfaces need not block independent contract implementation.

### Located call graph

| Source | Existing responsibility and preserved behavior | Proposed owner or treatment |
| --- | --- | --- |
| `.claude/skills/sync-with-dev/SKILL.md:14–18`; `scripts/merge-to-dev.sh:7–11,629–665` | `/sync-with-dev` invokes the worker's foreground merge; the lead supplies a worktree argument for a missing worker. | Keep names, argument flow and one foreground result. No new merge queue or lead signoff. |
| `scripts/merge-to-dev.sh:263–358,513–528` | Refuse dirty/main/dev/detached inputs, foreign branch commits, migration conflicts and lost work. Prechecks do not rebase; a behind-dev warning prevents false attribution. | Preserve the existing algorithm and exact outcome classes. |
| `scripts/merge-to-dev.sh:385–490,532–555`; `scripts/with-heavy-test-lock.sh:39–64` | Serialize merges, rebase, carry exact-head checks, distinguish interrupted checks from red, compare named test-only failures with dev and refuse changed heads. | Preserve lock and test behavior; add current authority at the bounded Git effect, not a second scheduler. |
| `scripts/sync-with-dev/land.sh:62–121` | Verify branch/dev inputs, protect a checkout used by a pipeline and avoid overwriting uncommitted work; fast-forward dev or update it using the old ref value. | Keep those checks and operations. A trusted local broker executes only the registered final Git effect under the core work boundary. |
| `scripts/sync-with-dev/land.sh:127–132` | Install dependencies, migrate the main local database, refresh changed runtime code and attempt the dev backup mirror. Failures warn after dev has moved. | Keep order and warning semantics. In fixtures, all targets are owned copies and the mirror is a run-owned bare repository. |
| `scripts/sync-with-dev/land.sh:135–148` | Invoke `linear_sync.py landed`, read deleted-code references, correct branch comments and inspect fix-proof declarations. | Replace writer entry points with narrow compatibility translation to reviewed application operations. Read-only findings remain observations; they cannot expand task scope. |
| `scripts/sync-with-dev/migrate-main.sh:74–88` | Report a local database behind/caught-up condition through `after_merge.py`. | Persist scoped observations and reviewed notification obligations; no direct legacy issue creation or closure. |
| `scripts/merge-to-dev.sh:175–200,557–625` | Stop the worker database before waiting, remove it after merge, retain warning-only cleanup failures, record quick proof and start heavy tests separately. | Let the route own its database sequence. Record expected absence without rebuilding it or running competing cleanup. |
| `scripts/sync-with-dev/postmerge-runner.sh:85–178` | Aggregate ranges, run heavy checks once, revisit dev, retain red/not-run distinctions, notify fix-forward work and clean eligible trees after releasing the lock. | Own a durable project background-job record, scoped callback outbox and resource leases. A finished worker does not prove this job stopped. |
| `scripts/worktrees/remove-merged.py:311–369,420–459` | Require content on dev, clean/unchanged heads, no process use, no lost work and database removal last before deleting eligible trees. | Preserve these safeguards and add installation/task resource ownership. A directory prefix or a process lookup alone is not authority. |
| `scripts/worktree-db.sh:225–265`; `scripts/test-stack.sh:630–677`; `scripts/worktree-db-sweep.sh:144–198,252–287` | Build from migrations/bootstrap, reserve room and sweep old stacks. The sweep enumerates daemon-wide `wt_*` resources and can remove a stopped stack with no known local claim. | Preserve build and capacity safeguards; restrict effects to a trusted owned-resource ledger. Unknown or foreign resources remain untouched. |
| `scripts/pipeline/lib/worktree-at.sh:21–49`; `scripts/sync-with-dev/lib.sh:16–21,43–128` | Share worktree-record locking, hook-free rebase, content-based ownership and immutable published-migration checks. | Reuse their contracts. All new project resource operations participate in the same repository lock; missing lock support must be explicit, never assumed safe concurrency. |

Other callers include `scripts/pipeline/prepare-contract-promotion.sh:118`, `promote-contract-migrations.mjs`, `pipeline-git.sh:213` and `pipeline-state.sh:417`. Contract promotion and fixes during a push still reach dev through this route. `#push`, `#release` and `#hotfix` retain their command intent, snapshots and separate deployment evidence; this task does not run those deployments.

### Conflicts and useful behavior

| Finding | Evidence | Consequence |
| --- | --- | --- |
| A configured merge callback override is incomplete | `merge-to-dev.sh:127` supports `MERGE_TO_DEV_ISSUES`, but `land.sh:135,141` directly calls writers. `migrate-main.sh:74` has a separate override. | Setting one environment variable cannot establish a clean cutover. Inventory and fence every callback and stale copy. |
| Legacy delivery callbacks do much more than record a merge | `linear_sync.py:7580–7673` updates statuses, descriptions, comments, dependencies and projects. `branch_notes.py:79–111` writes comments using independently selected credentials. | Replace their implementation, not their command surface. The guarded core owns status decisions and the existing reviewed writer/provider paths own prose. |
| A copied repository does not isolate Docker cleanup | `worktree-db-sweep.sh:146,175–186,278–279` reads daemon-wide stacks and can remove one unknown to this repository. Both database startup paths invoke it. | No real stack test may use the current sweep against the shared daemon. Require scoped removal or a separate confined daemon and independent foreign sentinels. |
| Known human decisions can arrive while a merge waits | The final operations in `land.sh:79–81,116` have Git checks but no coordinator lease boundary. | Gate the effect with current core authority. A launch check or post-result check alone is insufficient. This is an integration requirement, not a diagnosis that the old standalone script is broken. |
| The observed bridge boundary is insufficient for that effect | At `c19cf44`, `executions.py:31–37` supplies `MergeContext`; `supervisor/managed.py:211–230` separates authority checks from a resource boundary that still verifies the application database. | Request a short authority-only merge effect boundary from the bridge owner. Intentional database stop/removal must not invalidate a committed merge. |
| Two scenario rows still prescribe background/Monitor use | `.claude/standards/devops-part7.md:33,38` conflicts with `merge-to-dev.sh:7–9` and the approved foreground requirement. | Align those instructions during migration. Preserve the separate detached heavy-test obligation, not stale foreground-command wording. |

Recent source deltas add edition-data building to `stack-build.sh:581–628`, `worktree-db.sh:263` and `test-stack.sh:677`: seeded published editions with answers must produce snapshots even while cron launches are off; an empty/no-answer fixture needs no such build. The latest shape checks inspect the actual handed database. Monitor waiver changes in `his_waiver.py`, `waive-pause.sh` and `pretool-monitor-loop-is-held.py` preserve explicit, bounded human pause decisions; they do not authorize a task-state bypass or deployment here. These changes belong in migration inputs and isolated checks, not in an assumed old database fixture.

Retain partial-issue behavior: `linear_sync.py:2577–2587` recognizes `Part-of`, so a merged piece is not whole-issue completion. Retain exact-head, whole-file and carried-result distinctions in `ran_record.py` and the CG-3397 regressions; a pass carried from another commit cannot fabricate fresh proof. Keep lost-write obligations and warning-only callback failures durable rather than restoring the legacy writer.

### Wider integration findings

The table identifies demonstrated intersections with the new coordinator. Retain application behavior outside those intersections; do not remove a whole folder because one task writer is obsolete.

| Located surface | Current behavior or conflict | Proposed ownership and treatment |
| --- | --- | --- |
| `CLAUDE.md`; `.claude/standards/task-management.md`; nested `CLAUDE.md`/`AGENTS.md` | Root read-links, actual nested imports and legacy role/status rules can reach the same model through different paths. Source instructions are evidence, not new user authorization. | Apply the approved product policy once. Replace obsolete task/role authority while retaining useful command, testing and work-protection rules. |
| `.claude/output-styles/lead-engineer.md`; inspected host settings | The available style already has `keep-coding-instructions: true`. A file on disk does not prove the effective session selected it. | The lead may investigate, prepare briefs, review evidence and do narrow coordination work; substantive application work is delegated. All worker communication passes through the lead. Verify actual selected/effective settings at activation. |
| `.claude/skills/monitor/SKILL.md:1261–1274,1398–1430` | Stable defect fingerprints and open-issue deduplication are useful. The skill still documents Low-priority Todo creation, native background agents and substantive lead takeover. | Keep diagnosis, file ownership and singular protected shipping. New managed work starts in Backlog, is explicitly queued, and receives an actual task-owned worker. Replace native hook authority and unrestricted takeover. |
| `.claude/skills/release-pipeline/SKILL.md:20–49`; `.claude/skills/test-tasks/driver.md:1–39` | Runnable step order/skips and open-run protection are useful. Native TaskCreate/TaskUpdate hooks currently cause subissue/status effects; one old test fixture backgrounds a merge. | Preserve the script's step semantics and foreground command flow. Derive display from managed facts; version obsolete evaluator expectations without altering old results. |
| `scripts/linear/linear_sync.py:310–334,2720–2760`; Git-object re-execution and old copies | Environment, checkout/main environment files and renewable app credentials provide independent write capability. Replacing one current file or epoch check cannot fence old executables. | The credential custodian must stop old processes, disable launch/renewal routes, and retire or isolate automated write grants before new activation. Preserve authorized human Linear edits. |
| `scripts/lib/test-database.sh:74–90`; `scripts/lib/hermetic-env.sh:43–65,113–126`; CLI/edge-secret helpers | Broad environment loading and fallback profiles can retain credentials beyond the key a test cleared. | Confine actual environment, home/tool configuration, secret sources and egress. A copied repository, read-only SQL option or unset API variable is insufficient. |
| `scripts/sync-with-dev/land.sh:79–84,116`; `.gitattributes`; `scripts/setup-git-merge-config.sh` | Reference updates can invoke configured hooks; checkout/rebase can invoke filters or drivers. Read-only metadata confirms installed database-dependent and npm merge drivers. | Review the exact final Git execution/configuration. Keep rebase/drivers/follow-main/install outside Core; preserve legitimate checks rather than silently suppress them. Unsupported expansion stays unadmitted. |
| `scripts/monitor/watch.sh:294–370,500–550`; `scripts/monitor/lib/env.sh:76–151` | The business watch pauses application processing after verified conditions, writes a capacity baseline and uses read-only query options. | Retain separately authorized monitoring and incident controls. A business processing pause is not task On Hold; terminal output is not lead-AI acknowledgement. |
| `scripts/lib/local-stack.sh:106–155`; `scripts/ops/prune-local-logflare.sh:71–86` | Patterned IDs/ports and a sole matching container can look owned without coordinator evidence. | Require exact registered daemon/container/volume/network/port ownership. Unknown resources and foreign sentinels stay unchanged. |
| `scripts/lib/flake-ledger.ts:62–129`; `scripts/pipeline/lib/worktree-at.sh:155–248`; dependency helpers | Test history can resolve to main-checkout artifacts; helpers may append/prune, remove temporary trees or install packages. | Bind each effect to its owned path/process/configuration. Preserve ledger/result and temporary-cleanup semantics; do not grant arbitrary paths or executable test seams. |
| `.claude/skills/refresh-settings/SKILL.md:1–24` | The opening explicitly disables bulk reseeding; the old body explains historical migrations. | Keep the retirement notice. Do not generate an active capability from historical command examples. |

### One authority boundary and a durable foreground operation

Use the single proposed port `MergeContext.authority_boundary`. It is not implemented in the inspected integrated bridge. Keep `resources`, `before_start`, `spawn_boundary`, `check_authority` and `operation_key`; the launch/resource boundary must not be reused after the script intentionally removes its application database.

The project adapter owns durable application-operation facts. The supervisor/native bridge owns invocation/accounting and the trusted short authority section; Core remains the task/attempt/human-decision authority. Reviewed writing and provider services own prose approval, sends, readbacks and realtime inbound changes. No adapter writes Core tables, copies the status matrix, or exports a generic evidence issuer.

Before the final local effect, persist the exact operation/task/attempt, source manifest, physical common-Git/worktree identity, checked head and expected old/new ref. Revalidate authority and the supported configuration in the short section. Preserve old-value CAS or the existing checked-out-dev dirty/pipeline/fast-forward checks. Fetch, rebase, test waits, drivers, follow-main, installation, migration, mirror push and provider work remain outside Core.

A local command name and a measured wait timeout do not prove a safe execution bound. The exact CAS and legacy fast-forward paths need separately reviewed constraints, measurement and overrun/stop reconciliation before admission. A timer does not prove the mutating child stopped. If a supported bound cannot be established, retain the unresolved gate; do not substitute a full-script Core transaction or claim safe release. Current absence of a hook is not an immutable exclusion of future hook/configuration effects.

`reconcile_stopped(attempt_id)` observes complete protected-operation/child quiescence; it does not signal, authorize discard or request a stop. Model return, EOF, window expiry and controller loss cannot erase an active or uncertain foreground merge. An explicit authorized stop is separate and phase-aware. Retain capacity and resource obligations until actual process/Git reconciliation establishes their state. A crash after Git commits recovers that existing fact; another merge advancing dev does not authorize duplicate execution.

Callback envelopes bind operation, source, phase/sequence, owned issue, range/head and result/content digests. Persist receipt before acknowledgement. Core applies its existing delivery/human/partial-work guards; writer/provider paths handle prose and uncertain sends. Keep queued notification, actual lead delivery, AI acknowledgement and user reporting as separate observed receipts. Deployment rollback is an operational fact: it cannot reopen or downgrade delivered work; any further work uses a new linked, reviewed Backlog issue.

### Staged admission

| Stage | Admitted evidence after exact scope review | Still excluded |
| --- | --- | --- |
| F0: independent adapter contracts | Typed recording doubles and an owned operation/callback journal; ordering, replay, scope and uncertainty checks. Exact proposed files and node bindings are in the [Test Specification (TS)](test_specification.md#independent-adapter-checks). | Real legacy imports, processes, Git, Docker, application databases, credentials, providers, native models and production proof issuance. |
| F1: confined Git/process fixture | An independent common Git repository, owned local mirror and exact confined process/configuration plan. | Authenticated upstream, normal profiles/secrets and unclassified hooks/filters/children. The supported final-effect bound remains a prerequisite. |
| F2: pinned application fixture | Actually executed source dependencies plus an owned confined database/daemon or reviewed resource filter and independent foreign sentinels. | The current broad sweeper on the shared daemon, private database clones, foreign resources and hosted/production credentials. |
| F3: real integration acceptance | Separately reviewed run-owned TST/provider and actual installed host/model cases with their own scope and ledgers. | Inferred live success, unobserved accounts/models and normal activation before tested clean cutover. |

Five boundaries gate affected fixtures: callback/write credentials; final Git authority/configuration; protected process/child lifetime; application/shared resource ownership; and inherited environment, package/CLI/configuration effects. Independent F0 implementation need not wait for every unrelated source branch, provided those surfaces remain explicitly unadmitted. F0 results cannot satisfy F1–F3 or final-cutover requirements.

## Options considered

### Option A: Wrap the existing route without changing its dependencies

A runner can preserve the foreground call and observe Git afterward. This supplies the missing execution adapter, but leaves direct legacy writers, broad sweeps and the authority race intact. It cannot satisfy R28, R31 or the work-lease requirement. Environment overrides alone are incomplete.

### Option B: Preserve the merge algorithm and adapt its effect boundaries

Keep the merge entry script as the baseline. Add focused compatibility replacements for callbacks, an owned-resource restriction and a bounded authority-aware Git effect. Persist intent, physical observations, callbacks and background ownership independently. Test the original route and each necessary adaptation in disposable copies before cutover.

This requires a narrow bridge extension and consumer-aware installation work. It preserves the working checks instead of importing the legacy synchronizer. Callback or cleanup settlement can remain pending after a successful Git merge, so the user-facing result must distinguish these facts.

### Option C: Replace the merge algorithm in the coordinator

A new implementation could own every Git/test step. The present evidence does not justify duplicating rebase safety, content ownership, migration checks, dev-red attribution, result carry and post-merge recovery. It adds regression risk without a demonstrated need to replace those parts.

## Recommended option (B)

Implement the project adapter and only the demonstrated boundary adaptations. Keep the top-level merge bytes unchanged unless a later reviewed case proves that impossible; a future change must retain its before-image and frozen scenarios. Do not import the legacy monolith, copy raw private source into the public package or introduce network microservices for module organization.

### Proposed interfaces and ownership

The adapter implements the existing `ProtectedMergeCommands.execute_registered(merge_ref, context)` and `reconcile_stopped(attempt_id)` port. A trusted registration binds exact task/attempt/scope, approved prose digest, owned Git/database identities, source/dependency manifest, command mode and recovery role. The model supplies an opaque reference, never paths, shell text, environment overrides, callback authority or a caller-selected capacity limit.

Before launch, persist an operation intent and validate registration, resource ownership, manifest and current authority. Invoke the existing `--checks`, merge or missing-worker recovery form using an argv sequence in the registered worktree; keep the merge foreground through its result. Retain the complete sanitized execution trace privately and expose scoped results. Never record credential values in transcripts or other records. Preserve `CHECKS PASSED`, `WOULD NOT MERGE`, `MERGED`, `NOT MERGED` and `NOTHING TO MERGE`; `CHECKS PASSED` is not a merge, and `NOTHING TO MERGE` needs independent content reconciliation.

The proposed additional context port is `MergeContext.authority_boundary`: a trusted context manager that validates and serializes current core authority around only the bounded registered Git effect, without requiring a running application database. The project broker keeps the original old/new ref and dirty-work checks. Known revocation before that boundary prevents the effect. A decision observed after dev moves fences later work while retaining the committed Git fact. Git, the coordinator and Linear do not share one atomic transaction; remote races and lost acknowledgements remain explicit.

Callback translation accepts only registered operation/range/head and owned issue identities. Persist the callback before acknowledging it, re-read versions and use public application ports. A merge observation alone cannot mark required work complete, settle an active worker, advance a partial issue, override a hold, or prove staging/production. Generated fix-forward/local-behind task prose and comment changes require actual current writer approval; failed or unavailable approval leaves an obligation pending. Preserve deduplication and the original nonfatal callback semantics.

Issue `merge` evidence only from independently reconciled Git facts bound to the exact task/prose and operation. Its focused verifier must reject fabricated, foreign, stale or ambiguous records. Compose this issuer with existing proof routes through their owner; no generic public proof-minting operation is added.

Planned product ownership is `src/ai_task_manager/adapters/projects/ai_index/`: typed registrations/results, source binding, foreground runner, durable merge observations, bounded Git effects, callback translation and owned resource integration remain focused modules. `tests/merge/` owns Git/process/callback checks; `tests/resources/ai_index/` owns actual application resource checks. The adapter has its own facts store and calls core interfaces; it does not write core tables or copy transition rules. The runtime/bridge owner retains host execution, general process ownership and the new authority boundary. Installation/cutover consumers stay coordinated with the separate migration and structure work.

### Isolation, recovery and delivery

Create independent Git copies with no authenticated upstream or push URL. Their only mirror target is a run-owned bare repository; remove inherited credentials, Git URL rewrites and host account environment. Build only the ordinary local application stack from the pinned migrations/bootstrap, with provider keys absent and cron settings verified. Do not use `test-stack.sh --live`, production readers or normal-session configuration.

Record resource IDs, physical identity, port/network claims, parent operation and ownership before effects. Filter all sweep/removal paths by that ledger, including descendants and background jobs. Foreign, missing or uncertain ownership retains resources and reservations. Test with unrelated stopped/running sentinels. After `MERGED`, database absence is expected; removal failure is a cleanup warning and a retained obligation, never a failed merge or automatic rebuild.

After a crash, reconcile actual Git refs/reflog, branch content, process identity and owned resources before any retry. A crash after dev moves must recover the existing merge fact without repeating the committed effect. Heavy checks retain their independent job, tested-head records, second look and scoped repair obligations after the worker ends. Clean cutover must fence old hooks, materialized scripts, old Git-object re-execution and background callers before new admission; installation and interrupted recovery require separate tested evidence.

[Git documents old-value verification for ref updates](https://git-scm.com/docs/git-update-ref); preserve that comparison rather than replacing it with an unchecked assignment. [Git's worktree documentation](https://git-scm.com/docs/git-worktree) distinguishes per-worktree metadata and locked/dirty removal. [Python's subprocess documentation](https://docs.python.org/3/library/subprocess.html) supports explicit argv/environment and waiting for completion; it does not provide cross-system commit certainty. These sources support the mechanisms, not an assertion that this proposed integration is already safe or accepted.

## Outcome

The lead reviewed and publicly presented the located conflicts and the proposed isolated adapter approach. Existing user authorization permits necessary audited adaptation while preserving command flow; the lead presentation is not a new user decision accepting an unproved implementation. Option B remains the recommended technical design. F0 requires its own registered assignment, exact allowed paths/test bindings and candidate review; no code or behavior fixture ran under this documentation phase.

All MC01–MC107 groups, eight host-boundary cases and earlier drafts/results remain retained. The [Test Specification (TS)](test_specification.md) includes the 29 extended variants, of which 16 were added in the latest audit phase. They are unrun proposals, not collected or passing tests. The source audit and real fixture/cutover gates remain incomplete.

The original 48-hour and USD 100 overall limits are historical and were explicitly lifted by the user, together with overall retry ceilings. Finite invocation deadlines, configured capacity, scoped accounting and evidence-supported bounded batches still apply. No model call or paid infrastructure is needed for these documents. Live TST effects, public release, normal activation, production and source-skill changes remain outside this phase.

Before activation, the required future How-to Guide (HTG), `migrate.md`, must bind verified backups, obsolete-writer removal/fencing before replacement, actual process/credential/renewal retirement, replacement validation and interrupted-cutover recovery. File replacement, a timer or an epoch flag alone cannot establish sole automated-writer authority. Keep human edits and unrelated application monitoring/deployment capabilities available under their own authorization. Unknown relevant routes remain explicit pending work, not silently safe exclusions.
