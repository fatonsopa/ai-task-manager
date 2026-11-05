# Initial workflow preparation

## Before you begin

Use this trusted operator procedure to write and queue initial approved work before the first task-bound owner starts. Source `7e4e4f4` includes the independently accepted preparation and capacity corrections. Ordinary CLI and MCP calls still require their configured owner; this explicit command is not a model tool or an automatic fallback.

A trusted installer must already have completed normal session setup with current same-session enrollment, signed provider history and a sealed private service configuration. Declare finite `capacity_policies`, for example `[{"host":"claude","host_limit":2,"project_limit":2}]`. Limits are positive integers, host entries are distinct and the project limit is consistent across hosts. Trusted setup installs these existing Core policies after its authority checks. Ordinary service loads only validate persisted rows; missing or different limits require trusted setup reconciliation.

Preparation requires a private physical owner directory, absent owner record/socket, no execution attempts and no pending, leased or uncertain sends for the session. It shares the owner's nonblocking setup lock. Once owner activation has recorded its permanent signed latch, deleting owner files cannot reopen preparation.

## Steps

1. Prepare one private JSON request with exactly `operation` and `data`. The data object uses the existing public operation contract. Allowed operations are `writing_prepare`, `writing_review`, `writing_finalize`, `task_submit`, `task_show`, `task_list`, and `task_action` with `action: "enqueue"`. Delivered follow-up submissions are refused.
2. Replace `CONFIG` and `REQUEST` with the sealed service configuration and request paths, then run:

   ```sh
   aitm --config CONFIG workflow prepare --input REQUEST
   ```

   For an initial read, the complete request is `{"operation":"task_list","data":{}}`. This reads only the current session's tasks. Writing, submission and enqueue retain their existing approval and exact provider-readback requirements; they can create or update a provider issue.
3. Complete the existing writing prepare/review/finalize sequence before submitting its exact approved task. Wait for confirmed Backlog submission before explicitly enqueueing to Todo. Each call records its exact request; an uncertain send blocks further preparation until reconciled.
4. After the approved task and reviewer authority are ready, use the separately sealed author installation to start the owner as described in the [independent review How-to Guide (HTG)](../independent_review/how_to_guide.md). Report an existing scoped event with the separate command in the [lead reporting How-to Guide (HTG)](../lead_reporting/how_to_guide.md).

## Confirm success

Read the returned operation result and confirm the task through `task_show` or `task_list` while preparation is still eligible. A submission must have its confirmed provider result; enqueue must reach confirmed Todo. Preparation does not launch workers, complete tasks, start an idle lead or continuously drain events.

The accepted installed scripted cycle covers intake, enqueue, native author, independent scripted review, Done and lead reporting. Its declared provider/native/operator fixtures do not establish actual model execution, real LP17 acceptance or normal-session activation.

## If it fails

Stop dependent preparation when enrollment, capacity, owner state or pending effects refuse. Preserve the original request, operation, provider outcome and signed history. Do not remove owner files, edit Core rows, reset the activation latch or renew a deadline to force a retry. Unknown owner state and unsettled sends require existing trusted reconciliation; a lost response does not prove rollback.

The original missing-owner diagnostic and capacity-publication failure remain historical evidence. Current source corrects those boundaries; genuine host/provider acceptance remains separately required. See the [Test Status Report (TSR)](../test_status_report.md).
