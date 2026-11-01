# AI Index merge compatibility checks

Status: Draft
Decision MERGE-BOUNDARY: frozen candidate v1 awaits lead review before code or fixture execution.
Test basis: R08 revision 3, R09, R14, R28, R31, R32 and R34; the [protected merge behavior](../spec_driven_development.md#protected-merge-and-command-behavior) and proposed [Decision Record (DR)](decision_record.md).
Test item: Product project adapter not yet implemented; application source `873155ac73f486912e4e9fe6e3daa719388e0665`, merge SHA-256 `b47d53d4f9861e2a22e1412dd1d9b8c48407bf177c232cb97ff4770acd307022`.
Approach and release criteria: [Test Plan (TP)](../test_plan.md). This local specification adds source-derived cases; it does not replace or change the 248 existing authored acceptance cases.

## Coverage and setup

Keep source hashes, this exact case text, procedures and acceptance oracles before implementation. Review actual test code and fault placement before execution. Report all cases initially `not_run`; preserve failed attempts, blocked facilities and separately versioned corrections. A written case or source inspection is not a passing execution.

Use four evidence levels. Level 1 checks registration, parsing, ownership, proof and callback contracts without provider effects. Level 2 runs real Git and foreground/background processes in independent disposable repositories, with named database/transport stand-ins. Level 3 uses real run-owned Supabase application and test stacks with independently observed Docker/SQL facts. Level 4 composes reviewed live TST callbacks and the installed hosts later, under their own exact scope and ledgers. A lower level cannot satisfy a higher one.

The level-2 repository has its own common Git directory, dev/worker branches, logs, temporary directory, locks, port/owner records and a bare mirror. Never keep an authenticated upstream origin or push URL; isolate Git system/global configuration and inherited credential/host variables. Verify actual remotes and transport policy before any effect, including mirror failure cases. Preserve source hooks/checks as required by each case; do not borrow the original checkout's mutable node_modules or environment files without a reviewed ownership boundary.

Level 3 is blocked until the owned-resource filter or separate confined daemon is reviewed and implemented, sufficient measured capacity/dependencies are available, and the lead approves exact fixture commands. The existing ordinary `up` and `test-stack.sh run` paths sweep daemon-wide worktree databases. Do not run them against the shared daemon unchanged. Use independent foreign sentinels and a pre/post resource inventory, not only the component's own cleanup report. Build from committed migrations/bootstrap; no private database clone, provider key, production reader or `--live` mode. Each task owns an application database independently of its coordinator fixture database.

### Existing regressions to retain

These source tests are authored regression evidence, not executions in this task. Run reviewed scoped selections only after approval. Keep their version-bound tests/oracles or replace them with independently equivalent checks when legacy storage/protocols are retired.

| Source checks | Invariant and new coverage |
| --- | --- |
| `merge-to-dev.test.sh` T1.1–T1.15, T1.30–T1.36 and the T1.11c variant | Passing/red/conflict/concurrent/foreign-head/migration/type/cleanup results; MC10–MC28. |
| `merge-to-dev.test.sh` T1.16–T1.29; `rebase_guard.test.py` | Lost work, hidden conflicts, declared drops, equivalent changes and backup lifetime; MC17–MC20, MC61. |
| `merge-to-dev.test.d/stopped-check.sh` T1.37–T1.40 | One machine-stop retry, no red retry and correct signal; MC26. |
| `merge-to-dev.test.sh` T2 plus `sync-with-dev/follow-main.test.sh`; `sync-with-dev-script.test.ts` | Main-folder following, dirty-work protection, ordered post-merge steps and mirror/local-database warnings; MC28–MC31. |
| `merge-to-dev.test.sh` T7/T8/T9; `after_merge.test.py` | Independent heavy jobs, second look, red/not-run, bounded groups, attribution and exact test-result proof; MC32–MC40. |
| `merge-check-says-the-branch-is-behind-dev.test.ts`; `linear_sync.test.d/cg_3314_partial_work.test.py`; `a-carried-pass-proves-only-what-ran.test.ts` | Behind-dev attribution, `Part-of`, exact-head/partial/carried test evidence; MC12, MC38–MC43. |
| `worktrees/remove-merged.test.sh`; `worktree-db.test.ts`; `worktree-db-sweep.test.ts`; worktree-record lock tests | Unknown/busy/dirty resources stay; database last, port/stack ownership and serialized worktree metadata; MC47–MC57. |
| `scripts/rules/merge-scenarios.ts` and `merge-outcome-rows.ts` | Inventory each reachable answer/exit/verdict and map it to a case. Existence/parity checks alone do not prove behavior; MC62. |

## Cases

Each MC ID is a case group. Execute every named variant separately with its own observed result and cleanup record. Setup refers to the evidence levels above; exact future test commands remain pending test-code review.

### Registration and effect authority

| ID / requirement | Preconditions and action | Observable expected outcome |
| --- | --- | --- |
| MC01 / R08, R14 | Level 1: register exact task/attempt/prose, resource and source manifest; invoke its opaque merge reference. | Only its argv/worktree/profile is selected; durable intent precedes launch. The registration is retained with the result. |
| MC02 / R08, R31 | Change task, attempt, principal/session, prose, root/common directory, database owner or physical identity separately. | Refusal before effects; no foreign files, resources or callback payload retained in the public result. |
| MC03 / R08, R34 | Change each script/dependency hash, overlay version or authority epoch; replay an old Git-object/materialized entry point. | Stale execution is fenced, never falls back to a legacy writer or a newly guessed baseline. |
| MC04 / R14 | Omit registration, durable intent, authority, precheck, Git observation, callback persistence, cleanup observation or settlement independently; swap adjacent order. | The relevant effect/proof/settlement is refused or remains explicitly pending. No omitted step is inferred from exit 0. |
| MC05 / R14 | Repeat an operation key with identical and changed inputs; race two invocations. | At most one foreground effect; changed input conflicts. Completed/uncertain operations are observed, not relaunched. |
| MC06 / R09, R14 | Hold, reassign away, revise prose, revoke scope or expire the deadline before launch and while waiting for the merge place. | Known revocation prevents the later Git effect; active-process uncertainty retains reservations. No lead approval stage is added. |
| MC07 / R14 | Controlled race: a known decision commits before, during and after the authority-only Git boundary. | The core boundary serializes known decisions/effect; a later decision fences future work. A committed merge remains a fact. No global Linear/Git atomicity is claimed. |
| MC08 / R31 | The script stops/removes its owned database before later authority checks and result handling. | Authority is checked in the control store; expected application-database absence cannot cause a false failed merge or rebuild. |
| MC09 / R08 | Invoke a missing adapter and present a missing-runner-only refactor justification. | Report an integration gap; reject that justification for replacing the merge algorithm. |

### Git route and command results

| ID / requirement | Preconditions and action | Observable expected outcome |
| --- | --- | --- |
| MC10 / R08 | Level 2: valid worker branch; run the existing precheck and foreground merge. | `CHECKS PASSED` records no merge; `MERGED` has exit 0 and independent exact Git/range/content evidence. |
| MC11 / R08 | Branch with no missing change; repeat after an equivalent cherry-pick on dev. | `NOTHING TO MERGE`, exit 0, no unnecessary checks; content/lost-work reconciliation is required before any delivery proof. |
| MC12 / R08 | Precheck behind dev with a dev-fixed non-test finding, then level branch and malformed behind count. | Precheck does not rebase or move tracked state; warning is preserved only when supported. No false branch attribution or invented count. |
| MC13 / R08 | Main checkout, dev checkout, detached head, invalid path, dirty tracked/untracked files and invalid option/arity variants. | Preserve refusal/usage results and Git state; no stash, commit, reset or speculative cleanup. |
| MC14 / R08 | Carry another branch's unmerged commits; vary parent branch existence and own-base evidence. | Preserve content/ownership refusal. Never merge the parent implicitly or silently broaden the assignment. |
| MC15 / R08 | Duplicate/out-of-order new migration; unchanged published migration; modified published migration; absent/stale local remote evidence. | Preserve ordering and byte-specific immutable-migration distinction; correct renumber advice only where applicable; no network fetch to invent proof. |
| MC16 / R08 | Rebase conflicts, fails without conflicted files, or changes the executing script. | Preserve abort/error detail and before-image; no half rebase. The loaded foreground route survives its source file changing. |
| MC17 / R08, R14 | Rebase loses work; retry unchanged; restore the change; independently declare a supported drop. | Lost work refuses and keeps backups; retry cannot become false `NOTHING TO MERGE`; restored/declared work follows existing rules. |
| MC18 / R08 | Same patch ID in another location; dev holds the actual change; dev rewrites the same location; moved block variants. | Content oracle distinguishes real loss/conflict from equivalent work, preserving each existing outcome. |
| MC19 / R08 | Lost-work observer crashes, is absent or cannot save the prior version. | Fail closed with the actual diagnostic; do not run later checks or remove the backup. |
| MC20 / R08 | Successful/refused merge with backups made before/after the judged version. | Remove only judged obsolete copies after proven merge; retain later, unmerged or unknown work. |
| MC21 / R08, R14 | Three merges race; holder is live, stale or has an unreadable identity; timeout the wait. | One merge check at a time; preserve holder/wait and exit-3 refusal semantics. Unknown ownership does not grant a competing run. |
| MC22 / R08 | Dev moves after early checks, during final validation and repeatedly through the existing retry bound. | Rebase/recheck only when required; preserve old-value conflict handling and the existing bounded refusal. No new queue. |
| MC23 / R08 | Branch commits or becomes dirty after checks. | `NOT MERGED`, exit 1; dev does not move to unchecked content. |
| MC24 / R08 | A named test is red only on branch; all named tests red on dev; mixed static/test red; unnameable or unreached checks. | Only supported test-only dev red permits merge. Preserve every failing/unreached check and warning. |
| MC25 / R08 | Exact-head early/full pass records, changed head, partial record and invalid/missing records. | Carry only eligible evidence; required checks run. A cached result is not fabricated as a fresh run. |
| MC26 / R08, R14 | Gate killed once then passes; killed twice with KILL/TERM; ordinary red. | One extra attempt only for actual machine stop; correct signal and no false test failure/flake. Ordinary red is not retried. |
| MC27 / R08 | Migration/types range on already-rebased and newly-rebased branches, stale and correct types. | Types checked at each relevant held head; stale types refuse without moving dev. |
| MC28 / R08, R32 | Dev checked out nowhere versus in a clean/dirty/sparse checkout; active pipeline variants. | Preserve reference-only or fast-forward path, dirty/untracked protection and existing pipeline refusals. No direct push to dev/staging/production. |
| MC29 / R08 | Install, main-local migrate, runtime refresh and callback each succeed/fail after dev moves. | Preserve invocation order and warning-only semantics. A completed Git merge is never reclassified as uncommitted. |
| MC30 / R08, R32 | Owned bare mirror accepts, rejects divergence, times out or is unreachable; include alternate push URL/global URL-rewrite escape attempts. | Only the owned mirror can be reached; warnings preserve the merge. Never touch an authenticated upstream or an unreviewed remote. |
| MC31 / R08 | Main-local database stays behind, then catches up; callback fails in either state. | Keep the warning in the merge answer and durable notification obligation; do not forge success or fail the merge. |

### Background checks, callbacks and issue scope

| ID / requirement | Preconditions and action | Observable expected outcome |
| --- | --- | --- |
| MC32 / R08, R14 | Level 2: foreground merge ends while heavy job waits; worker/parent exits. | Foreground result returns independently. The background job survives as intended and remains owned/observable; worker completion does not erase its lease. |
| MC33 / R08 | Two merges during one heavy job and a merge exactly while its lock releases. | One active runner covers the ranges with the preserved second look; no omitted new head or duplicate uncontrolled job. |
| MC34 / R08 | Heavy run green, red, infrastructure/not-run and stopped variants; active push/release. | Correct last-green mark and yield behavior; red retains fix-forward obligation, not-run is not red/pass; no automatic revert. |
| MC35 / R14 | Heavy lane hangs, exits before deadline, receives TERM/INT/HUP, or leaves children/reaper. | Preserve per-step stop/race handling; observe all owned descendants and resource recovery. Unknown stop retains obligations/reservations. |
| MC36 / R28 | Each legacy call: `landed`, branch-note correction, local-behind/caught-up, quick/heavy green, red, dev-red, flaky and deleted-code finding. | One allowlisted translator records exact scope/range/head before acknowledgement. No legacy state/prose writer executes, including indirect imports/fallbacks. |
| MC37 / R14, R34 | Crash before/after callback receipt, application, provider send and readback; duplicate/out-of-order callback. | Durable obligations/idempotency survive restart. Uncertain provider writes reconcile through the existing provider path without blind retry. |
| MC38 / R08 | Quick/heavy evidence has wrong head, partial file/named subset, partial lane, carried-from-same head, carried-from-other head or no readable record. | Independent exact-file oracle follows CG-3397/whole-file rules; no false fix closure or fresh execution claim. |
| MC39 / R28 | Same red repeats, new failure is unrelated, flake pass is lucky, or failure is consistent. | Preserve deduplication/attribution and flake distinctions; record a reviewed repair task obligation instead of unreviewed issue creation. |
| MC40 / R28 | Generated issue/comment needs writing approval; deny, omit, stale or wrong-task approval and lose provider reply. | No unreviewed prose effect; pending obligation remains. Successful merge and pending notification remain distinct. |
| MC41 / R09 | One declared part merges using `Part-of`; a different part/attempt is incomplete. | Record the part/attempt; do not mark all required work complete or advance the entire issue. |
| MC42 / R09 | Complete Git merge while worker is active, required work incomplete, a hold intervenes or observed provider state conflicts. | Retain merge evidence, but core guards prevent premature/unauthorized In Dev. Do not stop a real worker by changing a status field. |
| MC43 / R09 | Range names unbound, wrong-team/number, foreign-principal/project, delivered or multiple owned issues. | Import no foreign content or authority. Handle each explicitly bound issue independently; delivered follow-up uses a new linked Backlog issue. |
| MC44 / R09, R28 | A legacy callback would jump to staging/Done, free dependencies, rewrite prose or align a project. | Only approved shared application operations/evidence can apply the effect; a merge cannot manufacture deployment or whole-project completion. |
| MC45 / R14 | Observe Git result, local event, provider-confirmed status, CLI update, lead queued/delivered/AI acknowledgement and user report independently. | Each receipt requires its actual observation; callback delivery does not count as AI processing or public reporting. |
| MC46 / R32 | Normal worker and missing-worker lead recovery; contract promotion and a fix during push. | Preserve exact supported command forms/actor capability. No new approval queue, implicit deployment or untracked scope expansion. |

### Resource lifecycle, crash recovery and migration

| ID / requirement | Preconditions and action | Observable expected outcome |
| --- | --- | --- |
| MC47 / R31 | Owned worktree database running/stopped/absent; SQL client active; observer unavailable. | Stop/removal follows the existing route and refuses active/unknown use. A failed stop warns; refusal names the restart route when applicable. |
| MC48 / R31 | Merge succeeds and DB removal succeeds, fails or loses its reply. | Preserve `MERGED`; record removed/warning/uncertain cleanup separately. Do not rebuild or run competing deletion. |
| MC49 / R31 | Independently create foreign running/stopped/unclaimed stacks and claims beside owned ones; invoke every scoped sweep/start/cleanup path. | Every foreign sentinel is byte/identity unchanged. Unknown owner is retained, not converted to gone. No shared-daemon run until this guard is reviewed. |
| MC50 / R31 | Two owned startups race for a prefix/slot, collision occurs, memory/stack capacity is exceeded, or ownership changes. | Preserve atomic claims and room safeguards; trusted capacity remains finite. No port reuse, foreign deletion or model-selected limit. |
| MC51 / R31 | Level 3: build an actual application DB from the pinned migrations/bootstrap; inspect URLs, migrations, cron and local bindings. | The real owned stack is ready and isolated. A SQLite/file/process stand-in cannot satisfy this case. No shared or hosted address/provider key escapes. |
| MC52 / R31 | Seed has published answers versus no answers; force edition-builder failure and a false success without snapshot rows. | Latest source builds/verifies derived data or explicitly skips a no-answer fixture; missing rows fail the build. Do not claim an old fixture covers it. |
| MC53 / R31 | Real test stack passes, fails and is killed before/after build; inspect reaper, containers, volumes, network, ports and owner record. | Cleanup is independently proven for owned resources; owner record remains while removal is uncertain. No `--live`/production access. |
| MC54 / R31 | Eligible merged worktree; locked, busy, dirty, lost-work, changed-head and undeletable variants. | Preserve refusal order and all work. Only owned eligible resources can go; database removal remains last and is rechecked. |
| MC55 / R31 | Process observer fails, PID is reused or only a command-line/cwd prefix resembles the owner. | Never infer quiescence or remove resources from incomplete/ambiguous identity. Retain reservation and report recovery needed. |
| MC56 / R31 | Concurrent add/remove/prune through existing and new callers; missing lock support variant. | Shared repository metadata locking or explicit verified serialization prevents half-record reads; unsupported safety stays blocked, not silently unlocked. |
| MC57 / R14, R31 | Crash after each resource intent, DB stop, DB removal, branch removal or claim release. | Reconcile physical identity and ownership before idempotent continuation; no resurrection, foreign cleanup or lost unfinished work. |
| MC58 / R14 | Crash before Git change, after dev moves before answer/journal, and after answer before callback settlement; another merge advances dev meanwhile. | Reconstruct the existing committed operation using exact Git/range evidence. Never rerun a committed merge merely because its reply is missing. Ambiguity stays uncertain. |
| MC59 / R28, R34 | Rehearse cutover with active merge, stale copies, old callbacks and pending writes; interrupt removal/replacement/activation. | Admission remains fenced until verified backup/replacements and sole authority are established. Recovery never enables dual writers or loses post-cutover facts. |
| MC60 / R32 | Capture protected command/argument/help/result contracts before and after installation; invoke an old entry point after cutover. | Commands and intent retain one route; stale entry points fail closed or translate solely to the new core. Normal activation/production is not performed by the rehearsal. |
| MC61 / R08, R14 | Re-run every retained lost-work, selected-result, cleanup and stale-writer regression against the final candidate; inspect failure controls. | Each independent oracle detects its corresponding deliberate fault; a historical replay/control is not fresh passing behavior. |
| MC62 / R08 | Compare every source outcome/exit/verdict and migration inventory entry with case coverage; inject a new unmapped outcome. | No reachable outcome or writer/resource caller is silently omitted; unmapped additions block acceptance. Static parity is separate from actual execution. |
| MC63 / R08, R31 | Repeat the same approved Git/process matrix on macOS and real Linux; independently record tools, locks, platform and actual DB scope. | Report separate verdicts and unsupported facilities. A Linux container on this Mac is not native Linux service acceptance. |
| MC64 / R14 | Exhaust retry/time/capacity/spend limits, restart the runner and request cleanup while a merge is uncertain. | Original limits persist; stop new admission and retain unresolved effects safely. Do not kill/retry an unknown committed operation or fabricate completion. |

## Procedure and cleanup

1. Record the lead's actual decision, confirmed source pin, exact artifact/case hashes and owned paths. Preserve this v1 candidate before any correction. Review the future test code, fault injection and independent oracles; no fixture execution is authorized by this document alone.
2. Build the level-1 and level-2 harness in owned paths. Validate credentials/remotes, environment, source manifests, independent sentinels and cleanup. Run a scoped valid-path pilot, then omission/order, permission, failure, concurrency and interrupted-recovery cases. Retain each failed attempt before repair.
3. Review level-3 commands, daemon/resource isolation, run ledger, images, measured capacity and cleanup. Only then create actual application resources. Test one owned stack first; observe SQL and Docker directly. Expand to the required failure/concurrency variants within trusted limits. Never use broad Docker prune or the unfiltered old sweep.
4. Review any level-4 live TST/installed-host composition separately. Bind canonical identity/team/project/run/issue IDs and actual receiver/transport scope. Existing two-user or host blockers remain blockers; do not replace them with mechanical results.
5. For each attempt, preserve before/after Git refs/content/reflog, command/answer/exit, intent/outcome/callback IDs, actual worker/background process identities, resource/sentinel inventory and cleanup result. Keep private source, credentials, personal paths and raw sensitive output outside public artifacts.
6. Clean only ledger-owned resources after verdict capture and verified process quiescence; retain resources/claims when ownership or effect state is unknown. Confirm the original source checkout/refs and every foreign sentinel remain unchanged. Report pass/fail/blocked/not-run per case variant and evidence level.

The existing source harness supports scoped case IDs: `bash scripts/merge-to-dev.test.sh <case-id>...`. That is a verified legacy interface, not an instruction to execute it in the original checkout. New product test commands will be frozen with the reviewed executable harness; no nonexistent command is presented as available.

## Open prerequisites

Lead approval of the proposed boundary adaptation, exact callback ownership and authority-only bridge port is pending. Implementation and case execution remain gated by that review. The actual application-database matrix needs scoped cleanup or a confined daemon, capacity/dependency evidence and exact fixture approval. Live TST effects, installed-host acceptance, full cutover and normal activation remain separate gates. No new model invocation, paid infrastructure, source-skill edit, production action or additional worker is authorized.
