# Queued task selection

Type: How-to Guide (HTG). Scope: the installed task queries, guarded workflow start and ordinary managed lead's read-only queue access. DP06 genuine qualification and story acceptance remain separately recorded in the delivery evidence.

## Before you begin

Use an independently prepared, current installation with the exact reviewed runtime and locked dependencies. Keep the sealed service configuration, selected account, finite authority, registered owner and original stores. The [initial workflow procedure](../workflow_preparation/how_to_guide.md) prepares approved tasks; the [recovery procedure](../recovery/how_to_guide.md) preserves interrupted owners and original effects.

Only explicitly queued tasks are candidates. Backlog, free capacity and a model's suggestion grant no permission to start work. Current dependencies, human decisions, task ownership and finite capacity still apply. This procedure does not claim that DP06's later declared-conflict or concurrent-delivery qualification is complete.

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

4. Read `bypasses`, `eligible_since`, `wait_seconds`, `fair_turn_due` and `waiting_reason`. Four committed later-queued higher-priority admissions promote the oldest continuously eligible qualifying waiter. Failed claims, capacity refusals and repeated requests do not count. Routine observations and restart preserve the waiting epoch; dependency loss, expiry or a current human hold ends it. No promotion preempts an active worker or overrides an admission guard.

5. Select only approved work that remains eligible. For an ordinary managed lead, the sealed installation exposes `task_list` and `task_show` through its existing read-only gateway before native startup. It can inspect the current explanation and report a task identifier. An event-only reporting installation does not receive those reads. Neither installation grants the lead worker-start, file or command authority.

6. The trusted operator runs the selected task through its existing installed plan. Put the exact task, plan reference and original operation key in `RUN_REQUEST`:

   ```json
   {"task_id":"<task-id>","plan_ref":"<trusted-plan-ref>","operation_key":"<original-key>"}
   ```

   ```sh
   aitm --config CONFIG --installation INSTALLATION --json workflow run --input RUN_REQUEST
   ```

   The owner rechecks current task/version, dependencies, human decisions and capacity. `admission_requires_capacity: true` means the queue explanation is not a slot reservation. A selection can therefore wait or refuse if facts change. Do not start a competing task to bypass that refusal.

## Confirm success

Compare the returned task/workflow identity with the selection and original key. A selected task, successful query, reservation or provider `In Progress` label alone does not prove actual worker start. Inspect the retained workflow, attempt, native/process startup and resource observations. Running requires the supported start evidence; completion additionally requires the task's declared delivery proof.

CLI `task list`/`task show`, MCP `task_list`/`task_show` and the application service expose the same scoped Core decision. The [Interface Contract (IC)](interface_contract.md) describes their fields and authority. Controlled protocol cases demonstrate projections and guarded admission; their authored worker/provider facts do not substitute for genuine managed-lead or worker observations.

## If it fails

Preserve the actual blocked, waiting, failed or unknown result and its release condition. Refresh only through the trusted source after a relevant authorized change. Failed or canceled predecessors do not satisfy a dependency, and a hold or reassignment cannot be removed to fill a slot.

After a lost reply, use the original request key or retained `workflow_ref` through supported resume/recovery. Observe the existing owner and effects before retrying; a timeout is not proof that no worker started. Preserve dirty, foreign, unmerged and uncertain resources. Missing authority, source continuity, account or stop observations block their dependent effects; follow the [recovery How-to Guide (HTG)](../recovery/how_to_guide.md) rather than replacing stores or reviving a revoked grant.
