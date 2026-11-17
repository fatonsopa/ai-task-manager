# Installed lead event reporting

## Before you begin

Use this bounded interface to report one existing scoped event through an already enrolled native lead. It is a component of the installed workflow, not a complete initial-setup procedure. DP04 independently accepted its genuine stock lead report at the recorded source and profile. T09 applies that mechanism to an original recovery event; current capture, startup and outcome evidence must retain their own exact bindings. Historical grants provide no current execution authority.

A trusted installer must supply private service and installation files, an existing Core session and task event, a pinned native manifest, and current provider enrollment. The manifest binds the selected account, model, executable and source. Authentication and native conformance need their own actual observations. Human-console mode also requires independent operator input; delegated mode requires explicit trusted permission. Configuration alone proves neither native execution nor human approval.

For an owner-independent report, the trusted installer stages a separate private configuration containing the original stores and still-valid Core capability, with `workflow_control`, `grant_ref` and `session_observations` omitted. Call `installation.sessions.reporting.setup(CONFIG, ORIGINAL_CONFIG, event_id=EVENT, deadline=DEADLINE, operation_key=KEY)` before sealing the lead. This distinct installer verifies the original completed setup, current provider binding, event, task version and retained physical sources. It grants only `session_show`, the selected `event_list` and its ordered receipts; task-action grants are empty. It preserves the original configuration, capability, revocations and deadlines. Generic session setup still refuses adoption, and reporting cannot restore a revoked Core capability.

Use a fresh finite reporting deadline and the existing notification evidence producers. Missing or replaced history, changed product source, foreign ownership, a changed task version or an expired reporting enrollment refuses consumption. A later report needs its own current authority; it cannot renew an old grant.

## Steps

1. Select the original recovery event from the supported public `event_list` projection. Preserve its event ID, task ID, current task version and original operation links in the evidence; do not create another event to stand in for a lost reply. Choose human-console or explicit delegated mode before sealing, as described in step 2. Have the trusted installer seal the installation with `role: "lead"` and a `lead` object containing `session_id`, `event_id`, `journal`, `key`, `directory` and `cwd`, with only the optional `operator_mode` selector in addition. The session and event must belong to the installation's task and scope. The other fields are absolute paths to the private host journal, signing key, fresh process directory and physical working directory.
2. Enroll the exact manifest through the trusted Python API `installation.native.enroll(..., role="lead")`. This is an installer API, not a shell command or model tool. Supply positive finite limits for `host`, `relay`, `auth`, `neutral` and `assignment`, explicit permitted identity-refresh names, and the existing conformance reference. Choose `lead.operator_mode` before sealing and enrollment: omitted or `"human-console"` requires the independent operator exchange; `"delegated"` explicitly permits this one finite event without a fresh approval prompt. The existing private permission repeats that choice. A lead role alone grants no delegation, and a model or reporting request cannot select the mode. The original finite installation deadline still applies. The stored permission is `normal-lead-v1`; worker permissions cannot substitute for it.
3. From a controlling terminal, run the installed command below. Replace `CONFIG` and `INSTALLATION` with the sealed private file paths. The command rejects request-input and session overrides.

   ```sh
   aitm --config CONFIG --installation INSTALLATION --json lead report
   ```

4. In human-console mode, complete the independent operator exchange for the actual authenticated startup and delegated actions. In delegated mode, the caller instead records a distinct signed `delegated_enrollment` with the exact current permission hash and physical source bindings. It records no human approval, operator process or delegated task actions. Its expiry cannot exceed the provider grant, native startup or finite permission. Both modes still require genuine native conformance and a controlling terminal for public output. Neither authorizes task completion or restores lost ownership. The caller then sends the exact projected event and its one-use acknowledgement challenge through the owned native transport. Its delegate permits only `acknowledge_event`, not worker file or command tools.

Live and cold consumption recheck current permission, mode, installation, scope, session, event and physical source. Expired, revoked or changed authority refuses new consumption.

The caller uses the existing public `event_list` projection before launch, after startup/operator capture, after reserving the event turn and before publication. A missing, wrong-task or quarantined event refuses. Raw retained Core event payload is not model input. These are fresh checks; they do not atomically exclude an independent ownership change after the check.

## Confirm success

The controlling terminal receives the public report; standard output receives the JSON result. Core must accept `delivered` and `ai_acknowledged` before first public output, then record the separate `user_reported` receipt for that terminal sink. No receipt proves another stage, a remote write or human acceptance of task work.

A coordination-chat report is a separate delivery. Check the actual report text against the event and recovery evidence using the pinned Writing Style (WS) guidance. The delivery lead publishes that checked text with the exact event identity, then records the visible chat delivery in the existing task evidence. A local terminal receipt does not prove chat delivery; keep a disconnected or unpublished chat stage pending.

The command owns one finite native lifecycle. Its `uses_worker` accounting field reserves the native process slot without creating a Core worker attempt. Confirm the owned stop result as well as the operation result; an unconfirmed stop refuses completion.

## If it fails

Preserve the original intent, receipt stages and process evidence. A second invocation of the same sealed startup refuses another launch or send and requires trusted reconciliation. A cold service can recover exact committed receipt operations without inventing replacement observations. Do not replace the journal, grant or deadline to force a retry.

For first-task intake before owner activation, use the explicit [workflow preparation How-to Guide (HTG)](../workflow_preparation/how_to_guide.md). That command and `lead report` are separate trusted calls. The earlier missing-owner diagnostic remains historical; the corrected source passed a declared scripted cycle, not genuine provider/model acceptance. Automatic idle-session wake, continuous event draining and the complete LP17/R36 cycle remain unaccepted. The [independent reviewer guide](../independent_review/how_to_guide.md) describes a separate authority and result.
