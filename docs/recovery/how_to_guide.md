# Recorded workflow recovery

Type: How-to Guide (HTG). Scope: current live-owner recovery and the DP05-T02 cold recovery candidate.

## Before you begin

Use the exact installed candidate package, original private service configuration and original installation file. Preserve the original workflow reference and operation key from the interrupted request. Live-owner observation and execution require current enrollment. Cold execution also requires the original unexpired native permission. The explicit cold stop port uses separate finite operator authority to settle an exactly recorded obligation, including after its execution grant expires or is revoked. Do not copy an installation or extend its deadline to make it eligible.

The candidate supports live-owner continuation, verified cold replacement and owned stop. Successors and resource retirement remain later DP05 work. Cold recovery requires the complete original installation, store, key and controller witnesses recorded before interruption. Older rows without those facts need explicit reconciliation; this procedure does not backfill them. Ordinary run/resume retains its existing contract. Genuine S1 qualification is still pending; follow the finite recipe below only after its exact inputs are admitted.

## Steps

1. Create a private request file containing exactly the original workflow reference and operation key, with `action` set to `inspect`:

   ```json
   {"action":"inspect","workflow_ref":"<original-workflow-ref>","operation_key":"<original-operation-key>"}
   ```

2. Run the stock installed command with the original private files:

   ```sh
   aitm --config <service.json> --installation <installation.json> --json workflow recover --input <request.json>
   ```

   Expect the same workflow/task/plan/key/deadline, its attempt and step observations, and the verified owner state. No new worker or provider write is part of inspection. Stop if identity, scope, configuration, source or current authority is refused. Read `action_availability`: a cold observation can offer `stop`; `successor` and `retire` remain unavailable. A callable resume still requires the ordinary workflow guards; this observation grants no permission for an effect.

3. Review the reported state and current human instructions. If continuation remains appropriate, change only `action` to `resume` and run the same command. A verified cold replacement uses a different actual owner process, follows signed routing in the original authority journal and preserves the original configuration and execution deadline. The controller decides the next eligible step. A human hold, reassignment, changed scope, missing review or uncertain effect may keep it blocked.

   To stop an interrupted worker after its owner is verified dead, use `action: stop` with the same workflow reference and key. Recovery records a stop intent, asks the original retained harness to stop itself and observes every applicable component before Core settlement. The result must report complete quiescence. Missing native, authentication, container, reviewer or merge facts keep the obligation blocked. A recorded stop fences later execution of that same workflow.

   After repeated owner replacements, keep the same request. Recovery checks the retained author and reviewer controllers against their original signed activations and the verified replacement lineage. Do not rewrite a controller record to name the latest owner.

   If startup stopped before component registration, the workflow's start result may still be pending even though Core committed a reservation. Use the same `stop` request. A confirmed pre-launch disposition reports the original attempt, `disposition: unlaunched_reservation`, `components_started: false`, and whether a runtime claim was recorded. Core pauses that original reservation without creating worker or stop receipts. Missing original forward evidence or any ambiguous launch history keeps the reservation protected; do not create a replacement witness or run another worker to resolve it.

   The same pre-launch checks apply to a configured reviewer through its own original reservation key and journals. The parent stop remains blocked until every reviewer and other applicable consumer is reconciled.

   If the interrupted start committed only its signed intent and Core has no reservation, use `resume` only while current execution authority still permits it. Recovery keeps the original decision, key, deadline and first-owner witness, and records the replacement's actual ownership separately. A committed or uncertain reservation cannot use this path to launch another worker.

4. Preserve the returned workflow identity and separate observed result. Repeating this request continues or returns its recorded work; it does not authorize another attempt or resend an unknown effect.

## Confirm success

Compare the workflow/task/plan/key/deadline and existing attempt with the original request. Inspection must preserve attempt and effect counts. Resume must retain completed stages and produce the ordinary current result, including any required wait. Verify actual Core/CLI/provider facts for the stage being claimed. A command's zero exit or a recorded step is not proof of business completion, provider execution, lead acknowledgement or human acceptance.

## If it fails

Retain the exact command, original identities, deadline, result and current observations. A missing reply may follow an effect; use the same reference and key to inspect the recorded owner before deciding the next action. Do not reset journals, launch an owner outside the recovery interface, signal recorded PIDs, remove endpoints by assumption, copy resources or renew old authority.

An unknown owner, missing original witness, changed file or unavailable component observation remains blocked. An expired installation cannot resume execution. Keep retained work and resources protected when stop evidence is incomplete; a successful stop retains task worktrees, databases and original journals. The [workflow recovery Interface Contract (IC)](interface_contract.md) describes the current boundary.

## Prepare genuine interruption qualification

This is a planned qualification recipe. It has not run. Do not start its effects until the delivery lead independently reviews the candidate and admits the exact fresh source, sealed inputs, account, scope and finite permissions. This admission is part of the already approved DP05 execution.

1. Prepare at most two fresh independent supported fixtures, one for replacement/replay and one for interrupted owned stop. Use the accepted default-owner route in the [DP03 handoff](../plans/deliveries/DP03-handoff.md), Supported procedure steps 1–2, with fresh original files, database, operation keys, resource roots and permissions. Select plain unmodified Claude CLI, the signed-in default account, `claude-sonnet-5-5` and high effort. Verify the actual current profile/account and authority before effects. Historical installations, grants and revoked permissions are evidence only. Bound each fixture to one author native call and a 300-second total qualification window. Set its original execution/owner deadline to at most 180 seconds inside that window, reserving at least 120 seconds for separate stop authority, automatic owner closure and observation. Any tighter actual native/session limits remain binding.

2. Install the exact candidate wheel with locked dependencies and verify installed runtime bytes against committed source. Enroll the fresh scope through `installation.sessions.setup`, using the approved actions and finite deadline. Prepare the supported native registration and sealed installation using the existing fixture machinery. Start `installation.workflows.launch(config_file, installation_file, stop=stop_event, ready=ready_callback)` in a real owner process. The callbacks may observe readiness or request ordinary lifecycle stop; they must not replace native/provider/recovery handlers. Confirm the authenticated scoped CLI before work.

3. Run the literal installed command `aitm --config <service.json> --installation <installation.json> --json workflow run --input <run.json>`, where the private file contains exactly `task_id`, `plan_ref` and `operation_key`. Preserve the returned workflow reference, original key, task/plan/source/deadlines, physical store identities and attempt/effect counts. If the run reply is still pending, obtain only the exact recorded reference through `WorkflowStore.open_existing(...).find(scope, task_id, operation_key)`; do not invent one or create another run. In the replacement fixture, wait for an actual retained safe workflow boundary, such as a completed native result awaiting review. In the stop fixture, require actual forward-recorded native identity, a running Core attempt and its recorded active assignment invocation before killing the owner. Stop if either required observation does not occur inside the original bound; do not manufacture it.

4. Kill only the exact test-owned owner through the original subprocess handle, then wait for that handle to report termination. Preserve its PID/start identity and original descriptor/socket. Do not kill discovered numeric PIDs or delete endpoints. Use the original installed `workflow recover` command and three-field request from this guide: inspect first, then `resume` for replacement/replay or `stop` for settlement. The completed T02 operator must verify the old owner's death and current applicable authority; stop authority must not refresh execution. No changed config, renewed old permission, second reservation or second native call is allowed.

5. Require the replacement fixture to show a different actual owner PID/start identity, the original task/workflow/key/plan/source/deadlines and identical retained attempt/effect history, plus a usable original-key replay through the installed command. Require the stop fixture to show actual native, command, waiting-harness, collector and merge component facts wherever applicable before settling Core; unknown components retain uncertainty and capacity/resources. Exercise a current hold/cancel at the retained boundary and verify it blocks obsolete execution. Compare fresh provider/Core/CLI observations rather than accepting a command exit or safe refusal as S1 completion.

6. Close only the genuinely owned remaining processes through supported lifecycle/stop paths. Record actual worktree/database/provider disposition and retain dirty, shared, candidate, uncertain and foreign resources. Preserve failed commands, original source/permission/deadline bindings and every uncertain effect. Do not erase journals or repeat an unknown provider write. Stop before the admitted deadline if the required connection or evidence is missing; return the exact remaining boundary for continued T02 work.
