# DP05 recovery increment and DP06 handoff

Scope: DP05 — Recover interrupted and expired work, based on accepted DP04 `d71dbf71c85fc02423833bcc17840d4482c3a296`.
Basis: the approved [Implementation Plan (IP)](DP05.md), its ten tasks, four stories and six epic criteria. The user approved version 1 on 16 November 2026. This handoff combines the supported procedure and Test Completion Report (TCR) for the 16–17 November 2026 UTC reporting period. The plan and final independent assessment own acceptance; this handoff supplies no new runtime authority.

## Delivered behavior

The installed operator can inspect an interrupted workflow, reconnect to a healthy owner, replace a verified stopped owner, reconcile original provider/Git effects, continue partial work, stop owned work and retire eligible resources. Expired or revoked execution requires a separately authorized successor for the reconciled remaining scope. Original grants remain expired or revoked. The narrow earlier zero-effect successor contract remains supported; it does not establish the broader behavior by itself.

Recovery retains the original task, workflow, operation keys, attempts, failures, deadlines, dirty work, candidate/check/publication history and physical resource identities. Known completed effects use original-key readback and replay. Unknown effects block dependent actions. Cancellation, current holds, reassignment and scope changes fence obsolete work. Foreign, changed, dirty, unmerged or unproven resources stay protected.

The reporting installer can attach fresh finite event-only reporting authority to an eligible original Core session and event. It grants no task actions, does not adopt a capability through generic setup and cannot restore a revoked Core capability. Delivery, AI acknowledgement, terminal publication and the separate coordination-chat report retain distinct evidence. None proves human approval of task work.

## Exact source and package

| Binding | Value |
| --- | --- |
| Public delivery branch | `delivery/dp05` |
| Final runtime source | `b23f9b9a7b79ee3d3abc023d0077d3f71222cd63` |
| Wheel SHA-256 | `6fdc4f89e2f2b1ca7fa56761810ccbb4d158b15c92e3d7b75a12060bdfbade00` |
| Runtime inventory | 350 source/wheel/noneditable installed files: 349 Python files and one data file |
| Runtime map SHA-256 | `e5b12203744f50287e79c3e43e7fb41fe461841574b568f82c7bdb4eddb85e53` |
| Runtime dependency inventory | 40 locked distributions |
| `uv.lock` SHA-256 | `57aa245a4610fb5671c51c34bb46d83d8d1d8e3f14d382dea99b1497400bcfb4` |
| Original package binding SHA-256 | `306b313d169e6983fb82a7cfaf85a5f6efd9b59e9d70203e2cb4c04794ae94d3` |
| Technical Writer | Pinned source `de5835afd77787f557c6ed0de277f9c507290f51`; original version-bound approvals remain retained |
| Final genuine native qualification | Unmodified Claude CLI `2.1.325`, selected default account, observed startup `claude-sonnet-5-5` |

Final documentation and acceptance commits preserve that runtime and lock. Requested effort is high; unobserved provider-internal model and effort are not established. The final package comparison checks every runtime file and installed distribution. Earlier genuine demonstrations retain their actual source/package bindings and accepted compatibility; they are not relabeled as executions of this final wheel. Qualification installations and revoked grants are retained evidence, never reusable live permission.

## Before you begin

Use independently provisioned configuration, current finite authority, the selected account and verified assignee/delegate/team/project. Retain original private configuration, installation, signed journals, controller witnesses and operation key. Confirm actual process ownership and current human instructions before dependent effects. Missing history is a reconciliation prerequisite; creating an empty replacement store is not recovery.

The [recovery How-to Guide (HTG)](../../recovery/how_to_guide.md) and [Interface Contract (IC)](../../recovery/interface_contract.md) define exact request schemas, installed composition, stop/retirement and version-two successor procedures. The [lead reporting How-to Guide (HTG)](../../lead_reporting/how_to_guide.md) defines reporting setup and the separate native command. These are supported APIs and literal CLI boundaries; the evidence does not establish every setup CLI variant or host/profile combination.

## Steps

1. Preserve the original workflow reference and operation key. Put them in a private request file with `action: "inspect"`, then run:

   ```sh
   aitm --config <service.json> --installation <installation.json> --json workflow recover --input <request.json>
   ```

   Confirm the same task/plan/key/deadline, attempts, owner and action availability. Inspection creates no worker or new execution grant. Refuse dependent work if identity, scope, store or current authority cannot be established.

2. Choose `resume` only when the current decision and declared scope permit continuation. A healthy owner retains its operation. Cold replacement requires the predecessor's verified stopped identity and original journals. Reconcile provider operations, inbound receipts, partial code, checks and publication before new effects. A lost reply is not proof that no effect occurred. Preserve original-key uncertainty when readback cannot decide it.
3. Choose `stop` to settle exactly recorded owned obligations. Check actual native, command, collector, containment and merge observations. A status, absent handle or PID alone cannot prove complete stop. Keep uncertainty and resources when required observations are unavailable.
4. If execution expired or was revoked, use the guide's version-two trusted successor procedure after effects, ownership and resources are reconciled. The fresh signed decision binds the exact predecessor, current task/version, remaining scope, required consumers and finite bounds. Equivalent retries return one successor. Never extend the predecessor's deadline or remove its revocation. A current hold, reassignment, delivered task or uncertain effect still refuses admission.
5. After the last consumer, use supported `retire` with exact owned resource identity and current retirement authority. Lost cleanup replies use the original key and readback. Preserve changed, foreign, dirty, unmerged, missing-history and uncertain resources; do not adopt them for cleanup.
6. For an eligible original event, establish the guide's current finite event-only reporting enrollment. Run from the controlling terminal:

   ```sh
   aitm --config <service.json> --installation <lead-installation.json> --json lead report
   ```

   Confirm distinct queued, delivered, AI-acknowledged and terminal user-reported receipts. Publish the checked report separately to the coordination chat and record that actual observation. A disconnected stage remains pending.
7. Stop owned processes, inspect attempts/leases/calls and retire current scoped authority after its last consumer. Preserve configuration, token files, journals, original deadlines and failed attempts as evidence. Record each remaining protected resource with its owner, missing prerequisite and resume action.

## Confirm success

Compare the actual task, original operations, provider facts, branch/worktree/database/fence identities and all receipt stages with the selected request. Exact completed replay preserves business identities, effects and native calls. The existing merge claim/release compare-and-swap counter may advance by two on completed replay; that administrative claim cycle is not a second business publication. A successful commit alone does not establish provider confirmation, resource retirement or user reporting.

## Testing and results

The fixed affected selection covers 307 distinct cases across 29 files, executed against the exact noneditable installed runtime. The original run recorded 287 passes, four failures and 16 setup errors in 483.32 seconds. Ten receiver/enqueue cases pass in 10.61 seconds after required runner bindings are supplied. Nine operator/reviewer cases pass in 20.10 seconds after three test-only fixture corrections: isolated children receive their actual pytest support path, and the authored cold-review guard accepts its current keyword. The unchanged continuity case passes in 6.90 seconds. Its original signed trace shows the shared temporary ancestor changed ctime during both permitted ACL observations; refusing that observation was correct. The actor causing the change remains unknown.

All 307 selected identities now have passing evidence: 287 original compatible passes plus the exact 20 corrections. This is an original failed run followed by bounded corrections, not one uninterrupted green run. The three test-only edits change no runtime file or locked dependency and require no new native qualification. Original failures remain retained. Other accepted task batches overlap this selection and are not added to its count.

| Criteria | Observed result and retained limit |
| --- | --- |
| E1-A1; T01–T02; S1 | Literal installed inspect/live-owner retry, genuine stopped-owner replacement and replay, active owned stop and current-valid cancellation fencing preserve original identity/deadline and avoid a second reservation/worker. Missing ownership observations stay unknown. |
| E1-A2; T03–T05; S2 | Original provider operations and inbound receipts reconcile without duplicate sends. Genuine partial-code publication, provider outage, lost acknowledgement and supervisor replacement/replay preserve one commit/candidate/check/publication and retained work. Cut to successful recovery measured 14.07 seconds in that fixture. |
| E1-A3; T06–T07; S3 | Atomic successor lineage and Core capability exposure retain one successor on concurrent/repeated requests. Genuine code expiry and a separate revoked predecessor complete through fresh authority, real author/two-reviewer work, Git/check/provider delivery and original-key replay. The original 150-second expiry and fresh 450-second deadline remain unchanged. |
| E1-A4; T02/T03/T06/T07 | Current cancellation, holds, reassignment, changed scope and missing ownership fence obsolete effects. Completed stops require physical settlement. Component controls cover unknown/foreign cases; genuine cases establish the stated selected-host boundaries. |
| E1-A5; T08; S4-A1/A2 | Exact installed stopped worktree/database retirement after Core revocation, lost reply and original-key replay. A separate genuine provider project create/archive reaches uncertain attempt 1, process-restart readback and replay without resend. Historical categories receive the plan's evidence-backed protected or closed dispositions. |
| E1-A6; T09–T10; S4-A3 | Final exact-artifact native qualification, original-event stock lead acknowledgement/publication, all four Core receipts and a separate actual coordination-chat report. Source/package, supported procedures, affected checks, measurements, independent assessment and DP06 boundary remain explicit. |

The final genuine qualification completed in 143.829097 seconds with all 17 calls stopped and probe permission revoked. The subsequent actual stock lead command completed for original event `b9eb29d0-d37d-467a-9c17-0925d9b0c1cc`; all five normal calls stopped. Original send `ad288ba2-60eb-4b5f-b056-54058f3892f6` and operation `fc0ca3bb-4648-42cb-85ac-571c3b069f33` remain confirmed at attempt 1. The query-only reporting observer used 254 of 400 reads. Core terminal reporting and Root's separate chat publication are separately observed; the chat receipt exposes no host-assigned message ID and proves neither human reading nor approval.

The accepted DP04 complete source run remains historical: 5,919 passes, 136 failures, 133 errors and 15 skips, followed by passing corrections for all 269 original failing/error identities and seven added cases. DP05 reuses compatible unchanged evidence with those bindings. It does not claim one uninterrupted green whole-project run or add overlapping task batches as independent coverage.

## Deviations and remaining risks

Original failed fixtures, rejected real candidates, helper/import/setup failures, expired windows, uncertain effects and withdrawn compatibility claims remain retained. T07's revoked-case frozen helper failure is preserved; independent raw comparison establishes the sole administrative CAS change. T09's generic setup refusal led to the explicit reporting installer, not removal of the no-adoption guard. Its direct-publication/business-consumer authority finding is fixed; completed historical publication replay remains supported. Earlier qualifications on superseded artifacts were revoked unused and remain attributed to their original bindings.

Current evidence covers the selected Claude account/profile and independently authored external-port controls where labeled. It does not establish all hosts/accounts, concurrent endurance, scheduling/performance budgets, continuous idle-session wake, the full release inventory or production readiness. Existing broad static diagnostic history remains historical; this delivery makes no global static-cleanliness claim. No normal-session activation, default-branch merge, production or original-application migration occurred.

## Resource disposition

The final report's 17 qualification and five normal calls are stopped. API/native/capture authority and the original Core capability are revoked after the last consumer; attempts and resource leases are zero. Token/configuration/journals remain intact. Its original provider issue/project stay protected because archive authority expired and event-only reporting grants no business/resource authority. The delivery lead owns later supported original-key disposition under current exact cleanup authority; no archive is inferred.

T08 retired fresh eligible owned local resources and confirmed the separate real provider archive. Original failed/genuine fixtures and their worktrees, stores, history and grants remain retained or protected under their accepted task dispositions. Dirty/unmerged work and foreign sentinels were preserved. Retained evidence is not an instruction to delete a fixture.

The [historical resource table](DP05.md#historical-resource-disposition) supplies each DP02/DP03/DP04 category, present outcome, responsible owner and exact prerequisite. It preserves uncertain DP03/DP04 archive keys, unknown decision-label effects, refused ownership and changed/foreign assignments. Locally recorded archive uncertainty is not a fresh remote archive observation. Missing compatible current authority prevents historical mutation; it does not justify recreating history or adopting the resource. Shared labels and Type metadata remain foreign workspace resources. Previously confirmed archives stay closed.

## If it fails

Stop only dependent effects, preserve the exact failed request and original journals, and inspect current facts before repeating an effect. A missing or uncertain create identity cannot be recovered by title search or resend. Do not reset accounting/history, switch accounts/models, revive revoked grants, extend old deadlines or replace a store to manufacture success. Keep independently eligible approved work moving; name the actual external prerequisite and responsible owner when no eligible work remains.

## Assessment and follow-up

The ten-task plan and independent cumulative assessment define the accepted DP05 boundary. Final acceptance requires independent closure readback of the existing fifteen mapped records; a task dispatch or report alone is not acceptance. The reviewed public delivery branch carries the cumulative increment. The private final task acceptance record binds the exact final assessment, checks, tracking readback and publication receipt.

DP06 is **Schedule and deliver concurrent tasks**. Its next session must read this handoff, retained scheduling/resource contracts and the existing roadmap, then prepare its own detailed plan for approval before implementation. Establish fair eligible progress, queue/capacity limits, concurrency/isolation, measured bottlenecks and finite scheduling/performance bounds on the cumulative installed product. Preserve recovery lineage, original effects, human fences and retained-resource protections. Verify environment prerequisites such as container/terminal support before claiming their coverage. Current DP05 measurements are fixture observations, not DP06 performance acceptance.

Do not reactivate old qualification permissions or historical fixtures as live authority. All-host gates remain DP07 and final release gates DP14. DP06 implementation, normal activation, production and default-branch integration remain outside this delivery.

## Sources

[Approved DP05 Implementation Plan (IP)](DP05.md); [DP04 handoff](DP04-handoff.md); [delivery roadmap](../incremental-delivery-proposal.md); [tracking map](../linear-tracking.md). Private primary proof remains in the existing `.local/delivery-learning/20261116-dp05-v1/` task/story acceptance records, final T10 assessment/checks and closure/publication receipts, with original source/genuine handoffs and package bindings in their owning worktrees. Machine paths, credentials and private account identifiers are excluded from this document.
