# DP01 session handoff

Prepared on 13 November 2026. This is a prompt for the user to submit to a new session. Saving or reading it as reference does not approve execution. The roadmap and detailed DP01 work retain the agreement boundaries stated below.

## Prompt

Work in `$HOME/projects/ai-task-management` as the accountable delivery lead.

For this session, I adopt the delivery order and working method in `docs/plans/incremental-delivery-proposal.md`, together with `docs/plans/delivery-learning-loop.md`. Preserve the original approved requirements and acceptance obligations. Your only delivery objective is **DP01 — Install and prepare approved tasks**. Develop its detailed plan first and present it for my agreement. After I approve that plan, implement, verify, independently review, integrate and demonstrate DP01. Do not start DP02 in this session.

### 1. Recover the current facts

Read `AGENTS.md`, `docs/readme.md`, the roadmap, learning protocol and `docs/audits/delivery-readiness-2026-11-13.md`. Then read the DP01-relevant sections of:

- `docs/implementation_plan.md`
- `docs/spec_driven_development.md`
- `docs/high_level_document.md`
- `docs/policy.md`
- `docs/test_plan.md`
- `docs/test_status_report.md`
- `config/requirements.json`, `config/execution-policy.json` and the relevant work/case inventory.

Read `.local/builds/20261101T155648Z/state.json`, its current referenced checkpoints and `.local/delivery-learning/20261113-planning-v1/checkpoint.json`. Follow the event/lesson journals and newer linked checkpoints if present. Inspect the actual Git state, worktrees, native agents/processes and relevant task/evidence records. Old running labels, PIDs, deadlines and results are historical until reconciled. Do not duplicate workers, signal an unverified process or renew an expired grant.

The audit baseline is `0285a8fda6067210c687d35b019a235fbaf30676`; inspect relevant drift. Reuse the existing audit and implementation. Investigate only DP01 gaps or changed facts, rather than repeating a whole-project audit. Preserve unrelated edits, screenshots, branches, failures, accounting and original run identity. Some planning files are uncommitted; inspect the working copy, not only Git HEAD.

### 2. Produce the detailed DP01 plan

Use the current Technical Writer source to write `docs/plans/deliveries/DP01.md`, titled exactly **DP01 — Install and prepare approved tasks**. If it already exists, reconcile its status and evidence before changing or repeating it.

Define one bounded epic outcome, its small usable delivery stories and their engineering tasks. For each, record stable IDs, scope, dependencies, existing implementation/evidence to reuse, missing work, interfaces, owned changes, acceptance criteria, shared Definition of Done, relevant checks and required completion evidence. Trace tasks to their story and epic criteria and to original requirement/case IDs. Keep unverified behavior and proposed commands explicit.

Follow `docs/implementation_plan.md#work-item-identity-and-traceability` for this and every later plan. Keep independent task IDs; preserve `DP01-T01` through `DP01-T08` if already allocated. Display Parent story and Criteria supported separately, and record the actual Linear identifier/link when known. A task can move between stories without renaming its identity or historical references. Link tests/evidence to criteria; task completion contributes evidence, while story closure requires all story acceptance criteria and integrated behavior to pass.

DP01's promised result is: in a fresh isolated project, an operator uses a packaged candidate and supported trusted setup, creates one approved real scoped Linear task in Backlog, explicitly queues it to Todo and observes authoritative matching state through the installed product. Include required scope/approval/replay/refusal behavior and owned cleanup.

Reuse the accepted Core, writing/intake/outbox, wheel and trusted setup work. The existing `aitm --config CONFIG workflow prepare --input REQUEST` interface is limited to initial preparation before first owner activation. Preserve its authority, private configuration, capacity, activation and unsettled-effect restrictions. Its trusted setup API is not proof of an existing public setup command. Define the missing supported operator interface from inspected code. Product worker execution belongs to DP02; development subagents may still help implement DP01 after approval.

Present the concrete plan, current gaps, supported demonstration, reuse decisions and unresolved material choices. **Wait for my agreement before product implementation, runtime tests or methodology trials.** Do not create detailed backlogs for the other 13 plans.

If the user has already approved this plan in its owning session, retain that approval and continue within its scope after current-state reconciliation. Do not ask again merely because task/parent/criteria relationships are clarified. Record a documentation-only amendment separately; preserve original source-bound approval/evidence and resolve only material scope or authority changes through their required approval path.

### 3. Execute after that agreement

Record the approval and reconcile the old phase-scheduling documents, work graph and admission controls with the adopted roadmap before new launches. Preserve their permission, ownership and evidence safeguards; changing the schedule cannot bypass them.

Finish one usable story at a time. Use registered subagents for bounded implementation and independent review when they help this objective. Each implementation worker owns one tracked task, branch/worktree and declared disposable database. Reconcile actual host capacity; the previously observed limit was three subagents plus the lead, not a guarantee of current capacity. Keep independent work parallel within that limit without inventing tasks to fill slots.

Require actual acknowledgement, confirmed task state and START. Consume each handoff, settle its obligations and dispatch the next eligible DP01 task promptly. The lead owns integration and the final result. Do not end work merely because a worker was dispatched or completed. Use bounded waits while authorized work remains.

Routine scoped implementation, tests, fixes, review and integration are authorized by my detailed-plan approval. Ask only for a material choice or an effect outside the actual authority; do not reopen permission for every test.

### 4. Verify task, story and epic outcomes

Every task must satisfy its own acceptance criteria and shared Definition of Done, with affected story/epic criteria assessed. Record whether it satisfies a parent criterion, contributes while the parent remains pending, or exposes a blocker.

Run focused checks appropriate to the change. Reuse valid source-bound evidence. Review complete changes independently; re-review only relevant changes or unresolved findings. Integrate promptly. Verify the complete story through its real interfaces, then verify the cumulative epic outcome. Task checkboxes, test counts or a review verdict cannot replace the integrated demonstration.

Use genuine Linear evidence wherever DP01 promises a real provider connection. Preserve all original full-product acceptance obligations for their delivery plans. Do not run unrelated broad suites, endurance or host-matrix work merely because old packets exist.

### 5. Keep the learning loop active

Continue the private learning records and link the original run. Record meaningful planning/action outcomes, expected versus observed behavior, candidate/evidence, review findings, actual handoffs, waiting, failures, corrections and next owner/action. Link existing primary logs rather than duplicating them. Keep unknown timing and usage unknown; redact secrets and private content.

Before planning each story or task, apply relevant lessons and record where they changed the work. At closure, assess whether those changes improved actual delivery and quality. Distinguish product defects, process failures, hypotheses and demonstrated improvements. Evaluate whether tasks lead to accepted stories and the epic result, not just whether agents stayed busy.

Use the existing progress guard after its scheduling contract is reconciled. Two unchanged failures or repeated intervals without meaningful advance require a concrete diagnosis and changed action. Do not rename retries, weaken criteria, restart healthy bounded checks or build another framework to evade a blocker.

### 6. Report and deliver

During active execution, report every 20 minutes using the exact DP01 deliverables: previous/current completion, change, working behavior completed, blocker/owner and next action. Keep other roadmap rows as concise status summaries and show actual acceptance separately. Count verified deliverables; never invent historical percentages or treat plan counts as effort. Keep the obsolete automation paused; do not claim a new scheduled report exists unless it has actually been configured for this session.

Group task rows by their story and show Task ID, Title, Parent story, Criteria supported and the actual Linear identifier/link when known. Preserve those identities across reassignment or reparenting; show any approved relationship change explicitly.

Mark completed parts `DONE -` with unchanged IDs and dated evidence. At DP01 closure, provide the integrated source/package identity, supported operator procedure, observed real demonstration, task/story/epic verdicts, relevant checks and review, cleanup or retained obligations, known limits and lessons for DP02.

Preserve all existing authority boundaries: standalone operation; AI Index strictly read-only with no copied scripts or retired merge alias; scoped test identities and resources; no credential exposure or changes; no silent account/model fallback; no normal-session activation or production effects. Do not build the future agent ecosystem, edit source skills or start ancillary projects.

After DP01 is delivered, leave a durable handoff for DP02 and return the result. If only external prerequisites remain after independent DP01 work is exhausted, report the exact need and preserve resumable state. Do not claim the whole product is complete.

Start now with source reconciliation and the detailed DP01 plan. Your first delivery is that concrete plan for agreement, not a generic methodology explanation.
