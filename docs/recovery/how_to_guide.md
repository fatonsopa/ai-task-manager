# Inspect or resume a recorded workflow

Type: How-to Guide (HTG). Scope: DP05-T01 live-owner recovery in the existing installation.

## Before you begin

Use the current installed package, original private service configuration and original installation file. The owner must still be live, its original installation deadline must remain valid, and its enrolled session must currently authorize observation. Preserve the original workflow reference and operation key from the interrupted request. Do not copy an installation or extend its deadline to make it eligible.

This procedure supports inspection and continuation through the same owner. Cold replacement, stopping, successors and retirement remain later DP05 work. The original signed activation must contain the installation/workflow/owner witness recorded by the current owner launcher. Older rows without that witness need explicit reconciliation; this procedure does not backfill them. Ordinary run/resume retains its existing contract.

## Steps

1. Create a private request file containing exactly the original workflow reference and operation key, with `action` set to `inspect`:

   ```json
   {"action":"inspect","workflow_ref":"<original-workflow-ref>","operation_key":"<original-operation-key>"}
   ```

2. Run the stock installed command with the original private files:

   ```sh
   aitm --config <service.json> --installation <installation.json> --json workflow recover --input <request.json>
   ```

   Expect the same workflow/task/plan/key/deadline, its attempt and step observations, and `owner_state: live`. No new worker or provider write is part of inspection. Stop if identity, scope, configuration, source or current authority is refused. Read `action_availability`: `inspect` and `resume` are callable, while `stop`, `successor` and `retire` are unavailable in T01. A callable resume still requires the ordinary workflow guards; this observation grants no permission for an effect.

3. Review the reported state and current human instructions. If continuation remains appropriate, change only `action` to `resume` and run the same command. The existing controller decides the next eligible step. A human hold, reassignment, changed scope, missing review or uncertain effect may keep it blocked.

4. Preserve the returned workflow identity and separate observed result. Repeating this request continues or returns its recorded work; it does not authorize another attempt or resend an unknown effect.

## Confirm success

Compare the workflow/task/plan/key/deadline and existing attempt with the original request. Inspection must preserve attempt and effect counts. Resume must retain completed stages and produce the ordinary current result, including any required wait. Verify actual Core/CLI/provider facts for the stage being claimed. A command's zero exit or a recorded step is not proof of business completion, provider execution, lead acknowledgement or human acceptance.

## If it fails

Retain the exact command, original identities, deadline, result and current observations. A missing reply may follow an effect; use the same reference and key to inspect the verified live owner before deciding the next action. Do not reset journals, elect another owner, stop a PID on assumption, remove a stale socket, copy resources or renew old authority.

A dead/reused/unknown owner, missing activation witness, expired installation, changed file or unavailable current observation remains blocked here. Keep retained work and resources protected for the later supported recovery path. The [workflow recovery Interface Contract (IC)](interface_contract.md) describes the exact current boundary.

## Prepare genuine interruption qualification

This is a planned qualification recipe for the remaining T02 implementation. It has not run. The optional harness component alone cannot perform it; do not start the effects until the installed cold owner/stop connection is complete and the delivery lead independently admits the exact fresh source, sealed inputs, account, scope and finite permissions. This admission is part of the already approved DP05 execution.

1. Prepare at most two fresh independent supported fixtures, one for replacement/replay and one for interrupted owned stop. Use the accepted default-owner route in the [DP03 handoff](../plans/deliveries/DP03-handoff.md), Supported procedure steps 1–2, with fresh original files, database, operation keys, resource roots and permissions. Select plain unmodified Claude CLI, the signed-in default account, `claude-sonnet-5-5` and high effort. Verify the actual current profile/account and authority before effects. Historical installations, grants and revoked permissions are evidence only. Bound each fixture to one author native call and a 300-second total qualification window, with explicit reserved stop/settlement time inside the admitted finite permission. Any tighter actual native/session limits remain binding.

2. Install the exact candidate wheel with locked dependencies and verify installed runtime bytes against committed source. Enroll the fresh scope through `installation.sessions.setup`, using the approved actions and finite deadline. Prepare the supported native registration and sealed installation using the existing fixture machinery. Start `installation.workflows.launch(config_file, installation_file, stop=stop_event, ready=ready_callback)` in a real owner process. The callbacks may observe readiness or request ordinary lifecycle stop; they must not replace native/provider/recovery handlers. Confirm the authenticated scoped CLI before work.

3. Run the literal installed command `aitm --config <service.json> --installation <installation.json> --json workflow run --input <run.json>`, where the private file contains exactly `task_id`, `plan_ref` and `operation_key`. Preserve the returned workflow reference, original key, task/plan/source/deadlines, physical store identities and attempt/effect counts. In the replacement fixture, wait for an actual retained safe workflow boundary, such as a completed native result awaiting review. In the stop fixture, wait for actual forward-recorded owned startup while the native/worker operation remains active. Stop if either required observation does not occur inside the original bound; do not manufacture it.

4. Kill only the exact test-owned owner through the original subprocess handle, then wait for that handle to report termination. Preserve its PID/start identity and original descriptor/socket. Do not kill discovered numeric PIDs or delete endpoints. Use the original installed `workflow recover` command and three-field request from this guide: inspect first, then `resume` for replacement/replay or `stop` for settlement. The completed T02 operator must verify the old owner's death and current applicable authority; stop authority must not refresh execution. No changed config, renewed old permission, second reservation or second native call is allowed.

5. Require the replacement fixture to show a different actual owner PID/start identity, the original task/workflow/key/plan/source/deadlines and identical retained attempt/effect history, plus a usable original-key replay through the installed command. Require the stop fixture to show actual native, command, waiting-harness, collector and merge component facts wherever applicable before settling Core; unknown components retain uncertainty and capacity/resources. Exercise a current hold/cancel at the retained boundary and verify it blocks obsolete execution. Compare fresh provider/Core/CLI observations rather than accepting a command exit or safe refusal as S1 completion.

6. Close only the genuinely owned remaining processes through supported lifecycle/stop paths. Record actual worktree/database/provider disposition and retain dirty, shared, candidate, uncertain and foreign resources. Preserve failed commands, original source/permission/deadline bindings and every uncertain effect. Do not erase journals or repeat an unknown provider write. Stop before the admitted deadline if the required connection or evidence is missing; return the exact remaining boundary for continued T02 work.
