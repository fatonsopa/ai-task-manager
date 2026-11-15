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
