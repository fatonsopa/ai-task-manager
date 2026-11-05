# Installed lead event reporting

## Before you begin

Use this bounded interface to report one existing scoped event through an already enrolled native lead. It is a component of the installed workflow, not a complete initial-setup procedure. Source `7e4e4f4` includes the independently reviewed caller, notification API, initial preparation and explicit delegated enrollment components. Genuine host, operator and public-user acceptance remains pending; see the [Test Status Report (TSR)](../test_status_report.md).

A trusted installer must supply private service and installation files, an existing Core session and task event, a pinned native manifest, and current provider enrollment. The manifest binds the selected account, model, executable and source. Authentication and native conformance need their own actual observations. Human-console mode also requires independent operator input; delegated mode requires explicit trusted permission. Configuration alone proves neither native execution nor human approval.

## Steps

1. Choose human-console or explicit delegated mode before sealing, as described in step 2. Have the trusted installer seal the installation with `role: "lead"` and a `lead` object containing `session_id`, `event_id`, `journal`, `key`, `directory` and `cwd`, with only the optional `operator_mode` selector in addition. The session and event must belong to the installation's task and scope. The other fields are absolute paths to the private host journal, signing key, fresh process directory and physical working directory.
2. Enroll the exact manifest through the trusted Python API `installation.native.enroll(..., role="lead")`. This is an installer API, not a shell command or model tool. Supply positive finite limits for `host`, `relay`, `auth`, `neutral` and `assignment`, explicit permitted identity-refresh names, and the existing conformance reference. Choose `lead.operator_mode` before sealing and enrollment: omitted or `"human-console"` requires the independent operator exchange; `"delegated"` explicitly permits this one finite event without a fresh approval prompt. The existing private permission repeats that choice. A lead role alone grants no delegation, and a model or reporting request cannot select the mode. The original finite installation deadline still applies. The stored permission is `normal-lead-v1`; worker permissions cannot substitute for it.
3. From a controlling terminal, run the installed command below. Replace `CONFIG` and `INSTALLATION` with the sealed private file paths. The command rejects request-input and session overrides.

   ```sh
   aitm --config CONFIG --installation INSTALLATION --json lead report
   ```

4. In human-console mode, complete the independent operator exchange for the actual authenticated startup and delegated actions. In delegated mode, the caller instead records a distinct signed `delegated_enrollment` with the exact current permission hash and physical source bindings. It records no human approval, operator process or delegated task actions. Its expiry cannot exceed the provider grant, native startup or finite permission. Both modes still require genuine native conformance and a controlling terminal for public output. Neither authorizes task completion or restores lost ownership. The caller then sends the exact projected event and its one-use acknowledgement challenge through the owned native transport. Its delegate permits only `acknowledge_event`, not worker file or command tools.

Live and cold consumption recheck current permission, mode, installation, scope, session, event and physical source. Expired, revoked or changed authority refuses new consumption.

The caller uses the existing public `event_list` projection before launch, after startup/operator capture, after reserving the event turn and before publication. A missing, wrong-task or quarantined event refuses. Raw retained Core event payload is not model input. These are fresh checks; they do not atomically exclude an independent ownership change after the check.

## Confirm success

The controlling terminal receives the public report; standard output receives the JSON result. Core must accept `delivered` and `ai_acknowledged` before first public output, then record the separate `user_reported` receipt. No receipt proves another stage, a remote write or human acceptance of task work.

The command owns one finite native lifecycle. Its `uses_worker` accounting field reserves the native process slot without creating a Core worker attempt. Confirm the owned stop result as well as the operation result; an unconfirmed stop refuses completion.

## If it fails

Preserve the original intent, receipt stages and process evidence. A second invocation of the same sealed startup refuses another launch or send and requires trusted reconciliation. A cold service can recover exact committed receipt operations without inventing replacement observations. Do not replace the journal, grant or deadline to force a retry.

For first-task intake before owner activation, use the explicit [workflow preparation How-to Guide (HTG)](../workflow_preparation/how_to_guide.md). That command and `lead report` are separate trusted calls. The earlier missing-owner diagnostic remains historical; the corrected source passed a declared scripted cycle, not genuine provider/model acceptance. Automatic idle-session wake, continuous event draining and the complete LP17/R36 cycle remain unaccepted. The [independent reviewer guide](../independent_review/how_to_guide.md) describes a separate authority and result.
