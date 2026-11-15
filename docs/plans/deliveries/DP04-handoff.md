# DP04 delivery handoff

Accepted on 15 November 2026. The approved delivery is **DP04 — Synchronize task changes and human decisions**, based on accepted DP03 `e41b8d452a3162496b8ffcc519347d2b455de034`. Acceptance is 9/9 tasks (100%), 4/4 stories (100%) and 6/6 epic criteria (100%). Fresh Linear readback confirms all 14 mapped records Done. DP05 implementation has not started.

## Delivered behavior

The cumulative installation can reuse an eligible existing Linear issue without creating another issue or silently queueing it. Explicit queueing and local changes use durable operations and separate authoritative readback. Authenticated inbound delivery wakes the drain immediately; registered-task polling recovers missed changes while preserving unknown attribution, human holds and lifecycle conflicts.

Human reassignment and changed delegation fence old actions. Changed prose needs exact approval through Technical Writer. An active worker's stop is established by runtime observations and settlement, separately from a human hold or remote status. Genuine decision requests use approved Decision Record (DR) content, the verified recipient, a recognized Linear mention and the managed decision label. Multiple requests keep that label until the last observed choice, preserve unrelated labels and retain original comment identity on replay. Routine progress has no decision-only behavior.

New issue descriptions retain their exact approved prose. Their type comes from the existing native Linear Type group through the approved Writer issue kind. Product selection requires explicit user instructions; the application does not predict it. Historical approvals retain their original content and metadata behavior. Development and qualification issues are created through terminal/API operations. The historical interface-created isolation fixture remains explicitly attributed to its original method.

## Exact source and package

| Binding | Value |
| --- | --- |
| Delivery branch | `delivery/dp04` |
| Runtime source | `1a7b819d602c8d747476d4bbe3c736c237c22fdb` |
| Integrated implementation and final fixture corrections | `95a2b1934cd2f34074bc23c73de0f8fea5abbf16` |
| Wheel SHA-256 | `083d3b21af7667e5e4ec92184d4055c40af9e4b8a902f4aae0f967e80629b192` |
| Installation manifest SHA-256 | `8394d2b9f961e94216c256bdf8c8eb66bf5669f79f682576f2a3b545d9884756` |
| Runtime inventory | 336 source/wheel/installed files; SHA-256 `fb8c77c7c583910b7352790b3897f217cb8df833984da178168e14d512ec8d90` |
| Runtime dependencies | 40 locked distributions; `uv.lock` SHA-256 `57aa245a4610fb5671c51c34bb46d83d8d1d8e3f14d382dea99b1497400bcfb4` |
| Technical Writer | Frozen version 4 from source `de5835afd77787f557c6ed0de277f9c507290f51`; historical version-bound approvals remain retained |
| Current native qualification | Unmodified Claude CLI `2.1.325`, explicitly selected signed-in default account, `claude-sonnet-5-5`, high effort |

The final fixture corrections affect test support and leave the installed runtime and dependency bindings unchanged. Subsequent documentation and acceptance commits must retain those runtime hashes. Qualification installations and revoked grants are evidence; they are not reusable live authority.

## Before you begin

Use an independently provisioned project, trusted configuration and fresh finite session/native permissions. Verify the actual account, human assignee, selected application delegate, workspace, team and project before effects. Preserve an original operation key and current scope/version for each action. Keep private configuration, credentials, journals and fixture ledgers outside committed documentation.

Use the [installed setup procedure](../../workflow_preparation/how_to_guide.md), [synchronization and decision procedures](../../readme.md) and [lead reporting procedure](../../lead_reporting/how_to_guide.md). These documents describe the supported APIs and commands. The demonstrations use real installed APIs and CLI operations; they do not establish literal execution of every setup CLI variant, continuous idle wake or all host/profile combinations.

## Steps

1. Establish the selected session through `installation.sessions.setup`. Where workflow ownership is configured, use the normal supported owner and preparation route. Do not run ordinary operations around that owner. Confirm the actual installed package, approved Writer policy and finite capability before admitting work.
2. For an existing issue, use installed find and inspect, then the trusted independent selection producer and exact prose approval before admit. Confirm the same canonical issue/history, no additional create and no implicit queue. Queue explicitly only after its current source and human instructions permit it. Delivered work needs a separately approved linked follow-up.
3. For new work or a local change, prepare, review and finalize the content through the pinned Technical Writer package, then submit the approved reference with the original operation key. Confirm durable local intent, immediate outbound execution and separate provider readback. A timeout or missing response remains uncertain until reconciled under its original identity.
4. Start the sealed installed receiver only with current enrollment and its authenticated HTTPS endpoint. Verify the receiver instance and actual readiness before enabling a webhook. After a provider change, compare authenticated receipt, Core application, installed task/event view and fresh provider facts. HTTP 200 alone does not prove application: duplicates and legitimate ignored deliveries can also receive it.
5. For a missed event, observe the next eligible registered recovery slice or restart the same receiver configuration while its original authority remains valid. Preserve the unknown actor/conflict if polling cannot establish attribution. A manual sync or a successful read does not establish a signed delivery or human instruction.
6. Inspect holds, assignments and changed prose before further work. Stop and reconcile the owned worker through the existing runtime contract; retain resources until all required stop components and actual quiescence are established. Use the trusted console capture and installed settlement operations for an explicit human instruction. Do not infer worker startup, delivery or acceptance from a Linear lifecycle status.
7. Use `task decision_prepare`, actual Writer review/finalization and `task decision_request` for a material choice. Keep options in the Writer's consecutive `Option A: Title`, `Option B: Title` format. Capture the responsible recipient's actual option through trusted `capture_instruction`, then resolve with the original key. Inspect the separate comment, choice and label stages; a recorded choice does not make an uncertain label mutation safe to resend.
8. Select the exact scoped event for a distinct installed lead permission. Run `aitm --config <service.json> --installation <lead-installation.json> --json lead report` from its controlling terminal. Verify queued, delivered, AI-acknowledged and user-reported stages separately, then report the observed result to the user. A pending/disconnected stage remains pending.
9. Stop only confirmed owned processes and independently inspect calls, attempts, leases and retained effects. Revoke stopped scoped authority and use supported original-key cleanup. Verify archive/delete readback or record the exact supported protection. Preserve foreign, unowned, changed or uncertain resources instead of adopting or removing them.

## Confirm success

Compare the exact current task, issue identity, assignee/delegate, prose, operation and receipt stages with the approved expectation. An exact replay must preserve canonical IDs and effect counts. Foreign task/event/decision/receipt access must refuse, and the other session's task must remain unchanged. Check the final candidate's package/runtime binding and criterion evidence rather than inferring acceptance from command exit alone.

The cumulative genuine isolation evidence is sequential: one account's signed-webhook path and the second account's registered polling path, together with shared-store negative operations. The second account's unknown-actor lifecycle change remains pending. This does not establish concurrent execution or a normal webhook delivery for each account.

## Testing and results

Reporting boundary: 15 November 2026, UTC, through final DP04 acceptance. The [approved Implementation Plan (IP)](DP04.md) owns task, story and epic criteria. Original accepted evidence keeps its original package binding; changed consumers received current installed observations.

| Evidence | Actual result and limit |
| --- | --- |
| T01–T02 / S1 / E1-A1 | Genuine installed existing Backlog/Todo intake, explicit queue, replay and separate provider readback; receiving installation created no issue or attempt |
| T03–T04 / S2 / E1-A2–A3 | Real signed delivery, invalid signature refusal, duplicate/replay protection and registered startup missed-change recovery |
| T05–T07 / S3 / E1-A4 | Active native work stopped after hold with observed quiescence; two actual operator choices, comment identity on replay, correct label lifetime, unrelated label preservation and recognized mentions; post-withdrawal foreign prose excluded from shared stores |
| T08 / S4-A1–A2 / E1-A5 | Two distinct real users, original 16 two-way negative operations and four current installed list/foreign-read checks; second-account poll changes only its task, other task unchanged, zero attempts/leases |
| Current native qualification | Eleven capability checks passed in 122.822170 seconds; all 17 calls stopped; no provider resources or business completion implied |
| Current installed follow-up file | All 25 cases passed in 35.61 seconds under the actual sealed installed interpreter. The later test-only move of identical Type metadata support to the existing provider parent preserves behavior; that compatible result is reused |
| Final application source gate | The complete original run reported 5,919 passes, 136 failures, 133 errors and 15 skips in 2,828.93 seconds. All 269 originally failing/error case identities now have passing correction evidence; 5,919 unaffected compatible passes are reused. Seven added negative/pagination cases also pass. Current source coverage is 6,195 passing cases and 15 explicitly scoped skips across 6,210 collected cases. This is a complete run followed by bounded corrections, not one uninterrupted green suite. |
| Final installed lead report | TST-337 event `598a253f-d925-4c81-9a70-af44a855eabc` has all four actual receipts. Stock controlling-terminal CLI returned zero in 166.114091 seconds; all five native calls stopped. The coordination chat reported the exact event. No business work or human approval is implied. |
| Independent cumulative assessment | All seven execution/readiness criteria met; 88 captured inputs match. Its structural acceptance check passes. Final publication and tracking closure are separate delivery actions. |

The final application checks use their actual execution contracts. The entire installed-only follow-up file passes under sealed P5. Seven final-identity cases pass under the actual interpreter of a disposable installed copy whose 336 runtime files and 40 distributions match P5. Other cases use the source test interpreter with explicit installed-consumer and environment bindings. Focused batches overlap and are not added as independent passes. The source coverage accounting and the 25 separate follow-up cases total 6,220 passing cases and 15 skips across 6,235 current cases; collection itself supplies no execution verdict.

Normal signed HTTP acknowledgements measured 20.53 and 16.87 milliseconds in T04. Registered startup recovery observed the deliberately missed change in 2.67 seconds. The later current installed owned-delivery, reassignment and foreign-prose acknowledgements measured 12.170, 14.934 and 24.198 milliseconds; an unregistered other-user delivery was ignored in 20.774 milliseconds. These are measured fixture intervals, not universal network service objectives. Recovery defaults to 300 seconds, supports 120–900 seconds and processes at most eight registered tasks per slice with a round-robin cursor, bounded backoff and webhook yielding.

## Deviations and remaining risks

The first final-suite attempt was interrupted after fixture failures and remains failed/incomplete. The later complete run also remains a failed original attempt. Its failures were resolved through current environment bindings, authored fixture corrections and approved contract expectations; runtime code and locked dependencies stayed unchanged. The correction groups retain original IDs, with the original reply-loss recovery function explicitly mapped to its new unpaginated and paginated variants. Shared-fixture consumers received affected checks. Seven Linux-container cases were not selected, seven private tmux layout cases lacked their fixture dependency, and one frozen-schema diagnosis remained optional. These skips establish neither container nor terminal/profile release acceptance; later delivery gates retain their obligations. The corrected fixtures preserve historical Type-prefixed content, author current content before approval, use the existing native-Type-capable provider and handle an absent optional module parent without weakening the forbidden-leaf guard. A source-interpreter invocation of the installed-only file correctly refused; it is not a passing installed result.

Two additional second-account normal-webhook trials failed endpoint qualification: public health returned 404 and the route returned 405. No webhook was created for those trials. Their configured retry cap does not mean three probes ran; the conclusive response ended each after one probe. The accepted isolation result uses the genuine second-account polling observation. The first case required late supported local administrative revocation after its original window; its expired flag and failure remain recorded.

The first controlling-terminal entry in the fresh reporting fixture passed its lead manifest through a worker-only workflow schema and failed before the stock lead command, startup intent or native call. The corrected entry uses the stock lead CLI and its own unchanged NativeManifest/lead-role validation. Original failed command evidence and the unused permission remain bound to their original deadline.

Optional broad static scans retain 133 Ruff diagnostics in files unchanged from accepted DP03 and 381 mypy diagnostics across 99 files, including 43 in changed files and two on added lines. These scans do not establish a clean global static baseline. They do not justify unrelated repository cleanup. The two added-line diagnostics concern the default `RejectProofs` fallback lacking `record`. The public decision handler requires the configured Writer and its scoped decision approval view before constructing the publisher. A direct default-constructed publisher would refuse through an attribute error before publication; global static/interface cleanup remains outside this delivery. Independent final review must assess that limited disposition.

The approved scope excludes complete recovery/successor handling, concurrency/endurance acceptance, all-host release, original application migration, normal-session activation, production changes and default-branch merge. Genuine operator-console observations identify the authorized automation operator; they do not claim a person's physical typing or human acceptance of a business result. The retained held-work result has no established model/business success.

## Resource disposition

The first reporting fixture’s cleanup observer refused after a real provider status change. A later pending-report preparation could not enter before the original ordinary reserve expired, so native enrollment did not start. Its provider/Core authority was revoked within the original window, with zero attempts, leases or native permissions; TST-336 and project `8b2d3e55-041b-4a40-b4f4-2c1c9170e560` remain protected. No old deadline or authority was reset. The fresh second reporting fixture completed the actual event/report path. Its normal lead permission and provider/Core scopes were revoked, with zero worker attempts/leases. TST-337 and project `8b5a6cff-4e5e-4139-bfea-076214fc33f5` were archived with confirmed original-key cleanup and no errors. Earlier accepted DP04 cases stopped/reaped their owned network/native processes and revoked their scoped authority. Owned issues/projects were archived and hooks deleted where the original-key contract permitted it. Historical failed source, qualification and cleanup attempts remain retained.

| Retained resource | Disposition and next owner/action |
| --- | --- |
| DP02 author pair and DP03 candidate/author pair, publication fence and journals | Protected accepted work; delivery lead must verify lineage and obtain current supported retirement authority before later disposition |
| DP03 v3 uncertain pre-HTTP archive and DP04 v2 TST-323/project | Original uncertainty/deadlines retained; delivery lead owns later qualified original-key recovery under DP05, with fresh prerequisite review and no create resend |
| TST-317/TST-318 and affected parent projects | Observer refused late ownership registration; unowned/protected, no retrofitted cleanup ownership |
| TST-326–TST-331 and their affected parent projects | Changed/foreign ownership protections retained; current assignment is preserved, including the reassignment exercised during isolation |
| TST-332/TST-333 and their project; TST-335 and its project | Unowned or changed metadata protected by the supported contract; TST-334 was separately archived with confirmed original-key readback |
| Decision-label candidate `10bbf8fc-e248-4699-8705-888a79f174ca` | Unobserved effect after GraphQL error; no resend or removal without supported original-identity reconciliation |
| TST-336 and project `8b2d3e55-041b-4a40-b4f4-2c1c9170e560` | Original cleanup ownership observation refused after a provider status change; API/Core scopes revoked, issue unowned and parent membership-protected |
| Shared Waiting for Decision label and native Type metadata | Existing workspace metadata, preserved; no inferred cleanup ownership |

The delivery lead owns retained fixture evidence and future supported reconciliation; actual current assignees retain ownership of their Linear work. Protected means deliberately retained under the contract. Unknown archive state is not successful removal, and process absence alone grants no cleanup authority. Review worktrees and original journals remain evidence.

## If it fails

Stop dependent effects when identity, scope, source, permission, process ownership or endpoint readiness cannot be established. Retain the exact input, original key, failed command and local/provider facts. Reconcile current state before repeating any side effect; do not reset a ledger, extend an old deadline, replace an authority store or switch account/model as a fallback. Keep eligible independent work moving within the approved scope.

For partial decision publication, inspect the original comment and label operation separately. For a foreign reassignment, preserve quarantine and the other user's content boundary. For a lost cleanup response, use the supported readback/recovery contract on the original identity. If that window has expired, preserve the uncertainty for qualified later recovery; this handoff supplies no new recovery authority.

## Assessment and follow-up

All approved task, story and epic criteria are accepted at their declared scope. Independent assessment accepts the current execution/readiness evidence, and fresh API readback confirms TST-315, TST-306 and TST-242 Done together with the eleven previously closed records. This checked handoff supplies the supported procedure and retained dispositions for the cumulative increment. Public source is the reviewed `delivery/dp04` branch; later documentation commits preserve the exact runtime/package binding.

DP05 is **Recover interrupted and expired work**. Its next session must first audit the retained current recovery interfaces and original evidence, then prepare its own detailed plan and obtain its required approval. Focus on owner/process restart, crashes, cancellation, partial effects, uncertain writes, retained resources and explicitly authorized successors. Preserve current IDs, provenance, deadlines, failed attempts and accepted safeguards. Do not reactivate archived fixtures or revoked grants, adopt foreign work, erase uncertainty or implement a competing task-state writer.

## Sources

The [DP04 Implementation Plan (IP)](DP04.md), [DP03 handoff](DP03-handoff.md), [delivery roadmap](../incremental-delivery-proposal.md) and [confirmed tracking map](../linear-tracking.md#dp04-approved-hierarchy) define scope and identity. Private proof remains under `.local/delivery-learning/20261115-dp04-v1/`: original task/story acceptance records, source/result review reports and acceptance checks, `t07-listener-integrated-binding.json`, `t07-live-v5`, `t07-live-binding-v1`, `t08-live-isolation-v1`, `t08-live-isolation-v2`, `t09-conformance-v1`, `t09-lead-v1`, `t09-lead-v2`, `t09-final-package-binding.json`, `t09-final-verification.json`, `t09-final-review-report.json`, `t09-final-review-check.json` and `t09-linear-closure.json`. Final source logs and fixture bindings remain in their owning worktrees. Credentials and machine-specific paths are excluded from this document.
