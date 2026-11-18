# Independent installed review

## Before you begin

This guide is for trusted installer code configuring the existing native review producer for an approved author task. Source `7e4e4f4` includes the independently reviewed `NormalReviewerFactory` and explicit initial preparation. The accepted scripted cycle exercises these components with declared fixtures; genuine model and provider acceptance remains separate. The current producer supports Claude and refuses unsupported hosts.

Use the configured account and model with actual authentication and captured native conformance. Keep reviewer authority separate from the author and [reporting lead](../lead_reporting/how_to_guide.md). An enrolled permission is permission to attempt a bounded operation, not evidence that a review or model call succeeded.

## Steps

1. Prepare a separate approved reviewer task and private state directory. Before first owner activation, follow the [workflow preparation How-to Guide (HTG)](../workflow_preparation/how_to_guide.md); ordinary calls continue to require the configured owner. The reviewer task must be non-code, confirmed `Todo`, unheld and currently owned. The author must remain owned, confirmed and unheld in `In Progress` or `In Review`, with the exact approved prose and no prior delivery.
2. Seal the reviewer installation with `role: "reviewer"`. Its `review_target` contains the author's `task_id`, `prose_digest` and `source_head`. Bind it to the same scope, repository and source as the author, a different task and state directory, and a deadline no later than the author's. A reviewer installation cannot select another reviewer. Include `resource_locks` with the current explicit lock pairs; use `[]` for a known empty set. The sealed `files` list supplies literal file claims. Missing declarations remain unknown and refuse admission.
3. Bind the author's `reviewer_installation` to that exact private file using `path`, `device`, `inode` and `sha256`. Separately enroll the reviewer manifest through `installation.native.enroll(..., role="reviewer")`. This trusted Python API requires positive finite limits for `host`, `relay`, `auth`, `neutral` and `assignment`, explicit identity-refresh names and a conformance reference. It creates `normal-reviewer-v1` permission; it does not bypass Core capacity, dependencies or work admission.
4. For an otherwise prepared author installation, use the existing owner command. `CONFIG` is the private service file and `INSTALLATION` is the author's sealed installation, not the reviewer's.

   ```sh
   aitm --config CONFIG --installation INSTALLATION --json workflow owner
   ```

The default resolver constructs `NormalReviewerFactory` from the sealed pointer and passes it to `NativeReviewSource`. Configuration cannot name a Python factory or supply an accepted verdict. Without the reviewer installation, the workflow remains waiting for semantic review.

## Confirm success

`NativeReviewCapture` starts a distinct tracked reviewer and supplies the immutable artifact inspection through the existing read-only gateway. Writable and command tools refuse. A published capture requires the actual protocol response and confirmed shutdown, and must match the inspection, criteria, native identity and profile. A capture is evidence for the workflow's review consumer, not task completion authority by itself.

Physical source checks run before the input transaction. Immediately before the complete input frame is released, the existing ownership rule checks the author using the Core work lease's active connection. This avoids a nested transaction and orders that release against committed Core holds or ownership changes. It does not provide a distributed lock against changes not yet observed from the provider. Foreign, inactive and closed connections refuse; this connection argument is internal, not model-visible JSON.

## If it fails

Retain rejected, malformed and uncertain outcomes. A refused input may have an incomplete staged prefix; it remains quarantined and is not a completed model request. An exact published capture can be recovered cold without another reviewer. An unresolved prior intent requires reconciliation, not another launch.

The original missing-owner diagnostic remains retained. Explicit preparation resolves that initial source boundary, and a separately recorded scripted owner cycle passed. Neither this procedure, the 20 earlier reviewer controls nor that declared cycle proves genuine model judgment or human approval. The earlier nested-transaction failure remains preserved. Current results and remaining acceptance are in the [Test Status Report (TSR)](../test_status_report.md).
