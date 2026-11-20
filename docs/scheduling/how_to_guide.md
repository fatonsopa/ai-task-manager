# Queued task selection

Type: How-to Guide (HTG). Scope: installed queue selection, concurrent start, verified dependency release, original-key recovery, reporting and owned resource disposition. The [DP06 handoff](../plans/deliveries/DP06-handoff.md) binds acceptance and measured limits to their actual candidate.

## Before you begin

Use an independently prepared, current installation with the exact reviewed runtime and locked dependencies. Keep the sealed service configuration, selected account, finite authority, registered owner and original stores. The [initial workflow procedure](../workflow_preparation/how_to_guide.md) prepares approved tasks; the [recovery procedure](../recovery/how_to_guide.md) preserves interrupted owners and original effects.

Only explicitly queued tasks are candidates. Backlog, free capacity and a model's suggestion grant no permission to start work. Current dependencies, human decisions, task ownership and finite capacity still apply. Keep each task’s installed plan, worktree, application database and operation key distinct. Closed concurrent members share one owner and merge authority; their transport handlers do not increase finite worker capacity.

Uppercase paths below are operator-supplied private files. Use the installed `aitm` executable from the intended project. A configuration with `workflow_control` routes ordinary calls through its retained owner; an unavailable owner is a recovery prerequisite, not permission to create another one.

## Steps

1. Read the configured session's current tasks:

   ```sh
   aitm --config CONFIG --json task list
   ```

   Inspect `observed_status`, `sync_state`, `human_hold` and the `scheduling` object. The list places selection-eligible tasks in admission order; blocked or unqueued tasks can remain visible afterward. Foreign or quarantined task prose is not available for selection.

2. Confirm the intended task through the same interface. Put its opaque identifier in `TASK_REQUEST`:

   ```json
   {"task_id":"<task-id>"}
   ```

   ```sh
   aitm --config CONFIG --json task show --input TASK_REQUEST
   ```

   `scheduling.state` describes dependency readiness. `selection_state: "ready"` identifies the current queue choice; `"waiting"` means another eligible task has precedence. An active attempt reports its recorded state. Check the accompanying hold, ownership and synchronization facts as well. A missing or expired dependency observation remains unknown or blocked.

3. If the declaration is unavailable or expired, let the trusted installed dependency integration obtain current provider observations before admission. Ordinary models cannot submit a replacement proof or assume an empty graph. The trusted service's `dependencies.refresh(task_id, operation_key)` is an installer/application API, not a new CLI command. Provider observation finishes before Core consumes its receipt.

   A dependency receipt lasts 30 seconds. A refresh performed before native startup can therefore expire before the lead reads the queue. Keep the trusted observation integration active within its original deadline and request budget during startup and selection, then stop it before the operator runs the task. Each collection needs a new key; replaying a key returns its original receipt and expiry. Check the current operator grant before and after each collection, preserve blocked results, and stop if the task is held, reassigned or no longer queued. This gives the lead no write or start authority.

   The trusted installer/application uses the existing API:

   ```python
   from ai_task_manager.application.workflows.inspection import current_grant

   service.seal.check()
   service.claim_store.check()
   current_grant(service)
   view = service.dependencies.refresh(task_id, new_collection_key)
   current_grant(service)
   ```

4. Read `bypasses`, `eligible_since`, `wait_seconds`, `fair_turn_due` and `waiting_reason`. Four committed later-queued higher-priority admissions promote the oldest continuously eligible qualifying waiter. Failed claims, capacity refusals and repeated requests do not count. Routine observations and restart preserve the waiting epoch; dependency loss, expiry or a current human hold ends it. No promotion preempts an active worker or overrides an admission guard.

5. Select only approved work that remains eligible. For an ordinary managed lead, the sealed installation exposes `task_list` and `task_show` through its existing read-only gateway before native startup. It can inspect the current explanation and report a task identifier. An event-only reporting installation does not receive those reads. Neither installation grants the lead worker-start, file or command authority.

6. The trusted operator runs the selected task through its existing installed plan. Put the exact task, plan reference and original operation key in `RUN_REQUEST`:

   ```json
   {"task_id":"<task-id>","plan_ref":"<trusted-plan-ref>","operation_key":"<original-key>"}
   ```

   ```sh
   aitm --config CONFIG --json workflow run --input RUN_REQUEST
   ```

   The retained owner supplies the installed workflow registration. Use `--installation` for its dedicated owner, lead-report or recovery command, rather than for `workflow run`. The owner rechecks current task/version, dependencies, human decisions and capacity. `admission_requires_capacity: true` means the queue explanation is not a slot reservation. A selection can therefore wait or refuse if facts change. Do not start a competing task to bypass that refusal.

## Continue delivery and recover safely

1. Start independently eligible closed members with their own original task/plan/key requests. Observe actual native assignment and attempt/resource identities. A dependency-bound task waits until its declared predecessor’s verified `code_merged` proof releases it. Provider status alone is insufficient. Collection prepares the current local merge witness after the provider read; neither that preparation nor a same-key receipt replay extends the original 30-second observation or five-second local proof.

2. If publication loses its target turn, retain the candidate, completed checks and cumulative repository wait. A public result with `data.reason: "candidate_target_moved"` identifies the narrowly verified target-only condition. Under still-current source, task, human, resource and finite authority, use the same original run request or `workflow resume` with `{"workflow_ref":"<original-ref>"}`. The existing workflow replans the candidate and resolves its required checks before publication. Eligible physical-origin passes may carry only when unit, inputs, catalog, binding, producer and source still match; stale or unbound passes refuse, and the author is not restarted. Generic `checks_blocked`, unknown physical state, source changes and holds do not authorize this continuation. Keep the 120-second cumulative repository wait across calls. The workflow can also handle verified empty/carried-only groups and known-complete post-preparation target movement internally. After an actual check nonce has been sent, only typed target drift may wait for that unit’s physical result; non-target guards stay active, and the next unit still requires strict validation. These paths add no public reason or physical execution and give the operator no permission to classify a generic or unknown failure as target-only.

3. After a missing reply, inspect the original workflow before another effect. If the owner is dead, use its original configuration and installation with a private recovery request:

   ```json
   {"action":"inspect","workflow_ref":"<original-ref>","operation_key":"<original-key>"}
   ```

   ```sh
   aitm --config CONFIG --installation INSTALLATION --json workflow recover --input RECOVERY_REQUEST
   ```

   Change only `action` to `"resume"` after the supported inspection establishes eligibility. A verified cold replacement can deliver an exact stopped durable commit when only the dependency observation expired: the original execution grant/deadline must still be current, and fresh ready dependencies must match the attempt’s original graph, source, prose and decision. Its signed internal proof preserves invalidation history; it permits neither another author nor a repeated completed commit. Missing evidence, changed inputs, holds or expired/revoked original authority refuse. Follow the [recovery procedure](../recovery/how_to_guide.md) for stop, retirement or an eligible separately authorized version-two successor. A fresh successor is not permission to adopt old grants or discard dependencies.

4. Confirm publication and canonical provider readback separately. Keep an exact saved publication or provider response for reconciliation; an older or unavailable read is not permission to resend. Read the original event through the installed `event list` projection. Keep the original Core capability usable until its final reporting consumer finishes.

5. Report that exact event using the [lead reporting procedure](../lead_reporting/how_to_guide.md). When a separate owner-independent phase is needed, the trusted installer uses `installation.sessions.reporting.setup` with a fresh private configuration, the original still-valid Core capability, exact event, fresh finite deadline and new reporting key. Seal and enroll its event-only lead, then run:

   ```sh
   aitm --config REPORT_CONFIG --installation REPORT_INSTALLATION --json lead report
   ```

   Verify terminal output, `delivered`, `ai_acknowledged` and `user_reported` receipts at their separate boundaries. Coordination-chat delivery additionally needs actual publication of the checked text and a subsequent acknowledgement. A receipt or native assertion cannot stand in for that publication or human acceptance. An expired reporting phase stays failed; a supported fresh phase has distinct authority and does not extend its clock.

6. After each resource’s last consumer, observe owned process/group stops, attempts, leases and pending effects, then revoke only the actual owned permissions and enrollments through their supported APIs. Retire eligible local resources through recovery `action: "retire"` under exact current cleanup authority. Provider archive has its own authority and original key. Keep journals, source bindings and dirty, unmerged, shared, foreign or uncertain resources protected. Record their owner, original identity, prerequisite and supported next action; the [DP06 handoff](../plans/deliveries/DP06-handoff.md#resource-disposition) carries the current retained obligations.

## Confirm success

Compare the returned task/workflow identity with the selection and original key. A selected task, successful query, reservation or provider `In Progress` label alone does not prove actual worker start. Inspect the retained workflow, attempt, native/process startup and resource observations. Running requires the supported start evidence; completion additionally requires the task's declared delivery proof.

CLI `task list`/`task show`, MCP `task_list`/`task_show` and the application service expose the same scoped Core decision. The [Interface Contract (IC)](interface_contract.md) describes their fields and authority. Controlled protocol cases demonstrate projections and guarded admission; their authored worker/provider facts do not substitute for genuine managed-lead or worker observations.

## If it fails

Preserve the actual blocked, waiting, failed or unknown result and its release condition. Refresh only through the trusted source after a relevant authorized change. Failed or canceled predecessors do not satisfy a dependency, and a hold or reassignment cannot be removed to fill a slot.

After a lost reply, use the original request key or retained `workflow_ref` through supported resume/recovery. Observe the existing owner and effects before retrying; a timeout is not proof that no worker started. Preserve dirty, foreign, unmerged and uncertain resources. Missing authority, source continuity, account or stop observations block their dependent effects; follow the [recovery How-to Guide (HTG)](../recovery/how_to_guide.md) rather than replacing stores or reviving a revoked grant.
