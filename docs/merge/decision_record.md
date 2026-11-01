# Project merges have effects outside task ownership

Status: Draft
Decision MERGE-BOUNDARY: proposed; awaiting lead review before code or fixture execution.
Scope: AI Index project adapter, MERGE-COMPATIBILITY / TST-84.

The approved coordinator requires one task-state authority, task-owned resources and recovery from uncertain effects. The existing merge route already supplies substantial Git and test safeguards. The missing project adapter is an implementation gap; it does not justify replacing that route.

Three located integration conflicts require focused changes to its dependencies: legacy callbacks can write Linear independently, database sweeps can remove resources outside the copied repository, and a blocking merge has no coordinator authority boundary at its eventual Git effect. Preserve `scripts/merge-to-dev.sh` as the default route and regression baseline. Adapt these boundaries rather than reproduce the merge algorithm.

The governing requirements are R08 revision 3, R09, R14, R28, R31, R32 and R34 in the [requirements registry](../config/requirements.json), together with the [protected merge behavior](../spec_driven_development.md#protected-merge-and-command-behavior). The [Test Specification (TS)](test_specification.md) defines the proposed frozen checks. No merge, application database, provider write or new implementation has run under this assignment.

## Source and evidence boundary

The inspected application commit is `873155ac73f486912e4e9fe6e3daa719388e0665`. The lead confirmed a fresh remote-only lookup still names that commit. The merge script SHA-256 is `b47d53d4f9861e2a22e1412dd1d9b8c48407bf177c232cb97ff4770acd307022`. Source locations below refer to that commit; reads used Git objects without changing the application checkout or refs.

The product baseline is `b384f10278b0ee6b0bcd72fef161e8327cdf491d`. Host bridge checkpoints `ede7e819` and `c19cf44` expose an observed integration port; they are separate source evidence, not proof of integrated or live acceptance. Private source/hash records remain outside public distribution. Public documentation paraphrases the contracts rather than copying private implementation.

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

Before launch, persist an operation intent and validate registration, resource ownership, manifest and current authority. Invoke the existing `--checks`, merge or missing-worker recovery form using an argv sequence in the registered worktree; keep the merge foreground through its result. Record the complete transcript privately and expose scoped results. Preserve all five answer classes; `CHECKS PASSED` is not a merge, and `NOTHING TO MERGE` needs independent content reconciliation.

The proposed additional context port is `merge_effect_boundary()`: a trusted context manager that validates and serializes current core authority around only the bounded registered Git effect, without requiring a running application database. The project broker keeps the original old/new ref and dirty-work checks. Known revocation before that boundary prevents the effect. A decision observed after dev moves fences later work while retaining the committed Git fact. Git, the coordinator and Linear do not share one atomic transaction; remote races and lost acknowledgements remain explicit.

Callback translation accepts only registered operation/range/head and owned issue identities. Persist the callback before acknowledging it, re-read versions and use public application ports. A merge observation alone cannot mark required work complete, settle an active worker, advance a partial issue, override a hold, or prove staging/production. Generated fix-forward/local-behind task prose and comment changes require actual current writer approval; failed or unavailable approval leaves an obligation pending. Preserve deduplication and the original nonfatal callback semantics.

Issue `merge` evidence only from independently reconciled Git facts bound to the exact task/prose and operation. Its focused verifier must reject fabricated, foreign, stale or ambiguous records. Compose this issuer with existing proof routes through their owner; no generic public proof-minting operation is added.

Planned product ownership is `src/ai_task_manager/adapters/projects/ai_index/`: typed registrations/results, source binding, foreground runner, durable merge observations, bounded Git effects, callback translation and owned resource integration remain focused modules. `tests/merge/` owns Git/process/callback checks; `tests/resources/ai_index/` owns actual application resource checks. The adapter has its own facts store and calls core interfaces; it does not write core tables or copy transition rules. The runtime/bridge owner retains host execution, general process ownership and the new authority boundary. Installation/cutover consumers stay coordinated with the separate migration and structure work.

### Isolation, recovery and delivery

Create independent Git copies with no authenticated upstream or push URL. Their only mirror target is a run-owned bare repository; remove inherited credentials, Git URL rewrites and host account environment. Build only the ordinary local application stack from the pinned migrations/bootstrap, with provider keys absent and cron settings verified. Do not use `test-stack.sh --live`, production readers or normal-session configuration.

Record resource IDs, physical identity, port/network claims, parent operation and ownership before effects. Filter all sweep/removal paths by that ledger, including descendants and background jobs. Foreign, missing or uncertain ownership retains resources and reservations. Test with unrelated stopped/running sentinels. After `MERGED`, database absence is expected; removal failure is a cleanup warning and a retained obligation, never a failed merge or automatic rebuild.

After a crash, reconcile actual Git refs/reflog, branch content, process identity and owned resources before any retry. A crash after dev moves must recover the existing merge fact without repeating the committed effect. Heavy checks retain their independent job, tested-head records, second look and scoped repair obligations after the worker ends. Clean cutover must fence old hooks, materialized scripts, old Git-object re-execution and background callers before new admission; installation and interrupted recovery require separate tested evidence.

[Git documents old-value verification for ref updates](https://git-scm.com/docs/git-update-ref); preserve that comparison rather than replacing it with an unchecked assignment. [Git's worktree documentation](https://git-scm.com/docs/git-worktree) distinguishes per-worktree metadata and locked/dirty removal. [Python's subprocess documentation](https://docs.python.org/3/library/subprocess.html) supports explicit argv/environment and waiting for completion; it does not provide cross-system commit certainty. These sources support the mechanisms, not an assertion that this proposed integration is already safe or accepted.

## Outcome

Pending lead review of this recommendation, the bridge boundary, exact callback ownership and the frozen cases. Code changes, fixture execution, live TST effects, public release, normal activation and production remain unapproved at this gate. The original 48-hour and USD 100 hard limits remain; no model call or paid infrastructure is needed for this proposal.
