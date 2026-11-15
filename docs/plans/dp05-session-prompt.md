# DP05 planning and execution prompt

Status: Planning handoff, 15 November 2026. DP04 is accepted. This prompt requests the detailed DP05 plan for one approval before implementation; it does not grant implementation approval.

## Prompt

Work in the AI Task Manager repository. Prepare **DP05 — Recover interrupted and expired work**, then execute only after I approve its detailed plan. Extend the cumulative accepted product through usable increments; preserve existing implementation and evidence.

### Goal and baseline

An interrupted supported task must resume or stop through the declared recovery interface without duplicate work, invented authority, lost changes or removal of foreign resources. Cover owner/process restart, crashes, cancellation, partial effects, uncertain writes, retained resources and explicitly authorized successors. Do not substitute a collection of safe refusals for the required supported recovery behavior.

Start from accepted `delivery/dp04`, observed at `d71dbf71c85fc02423833bcc17840d4482c3a296`, reconciling legitimate later changes. Read its canonical `docs/plans/deliveries/DP04-handoff.md` using `git show` if it is absent from the planning checkout. The supplied external DP04 handoff matched that canonical document exactly. Retained final review reports accepted coverage, and the closure record reports all 14 mapped records Done. DP04 acceptance is 9/9 tasks, 4/4 stories and 6/6 epic criteria, with its stated limits preserved. No fresh provider or runtime qualification was performed to prepare this prompt.

Read `AGENTS.md`, `docs/readme.md`, the DP05 roadmap row, and relevant sections of the seven governing documents: `docs/high_level_document.md`, `docs/spec_driven_development.md`, `docs/implementation_plan.md`, `docs/policy.md`, `docs/test_plan.md`, `docs/test_status_report.md` and `docs/readme.md`. Trace DP05 requirements and cases through their existing inventories; do not create another registry. Original requirement IDs and acceptance obligations remain binding.

Reconcile actual workers/processes, task ownership, Git/worktrees, controller state and retained effects before admission. Do not duplicate healthy work. Preserve the delivered runtime, dependency bindings and unrelated edits. Qualification installations and revoked grants are evidence, not reusable live permission.

### Reuse the bounded preliminary audit

The following observations came from source inspection on `delivery/dp04`, not new executions:

- `application/workflows/service.py` exposes ordinary run/resume and typed trusted `recover_expired`; `driver.py` checks original plan/runtime bindings and rejects expired initial admission.
- `application/workflows/recovery.py` supports one linked fresh successor only for an unchanged, confirmed queued non-code task with no prior attempt/effect and independently verified stopped-owner history. This is not general active, code-bearing or partial-effect recovery.
- `adapters/workflow_control/recovery.py` verifies original physical journals and stopped-owner identity rather than recreating missing history. `installation/sessions/recovery.py` retains enrollment/provider continuity and a finite trusted intake-recovery path.
- `supervisor/managed.py` can settle a recorded complete managed stop; missing child handles or incomplete stop observations retain uncertainty. `application/merging/workflows/recovery.py` supports bounded target-advance lineage and refuses to repurpose frozen publication history.
- Existing recovery/regression starting points include `tests/workflows/test_expired_recovery.py`, `tests/workflows/test_start_recovery.py`, `tests/linear/test_recovery.py`, `tests/resources/test_cleanup_boundaries.py`, `tests/receiver/test_installed_restart.py` and the related frozen contracts. Their presence is not a fresh pass or proof of installed recovery.

Paths above are under `src/ai_task_manager/` unless prefixed with `tests/`. Verify freshness and inspect only the additional callers, contracts and evidence needed to establish each gap. Do not repeat this discovery wholesale. Preserve valid controls while extending narrow contracts through reviewed changes; existing refusal behavior is not an oracle for all future recovery requirements.

### Prepare the detailed plan before implementation

Use current Technical Writer Implementation Plan (IP) guidance to write `docs/plans/deliveries/DP05.md`. Audit each relevant capability as already supported, needing connected evidence, defective, missing or blocked. Keep this assessment compact in the plan with primary evidence links. Do not run product tests, launch workers, mutate providers or reactivate historical fixtures merely to draft the plan.

Define one outcome epic and a small dependency-ordered set of stories/tasks from the actual gaps. Each story adds a supported, demonstrable recovery capability to the same installed product. Explain what a user invokes, what state/effects survive, what safely resumes or stops, and how success is observed. Distinguish API, literal CLI and actual host coverage; invent no command or private-helper success claim.

Give every task, story and epic explicit acceptance criteria and Definitions of Done. Map requirements to observable results and the smallest sufficient verification. Keep independent task IDs, explicit parent stories and separate criterion links. Reuse DP05's existing TST-243 plan record and tracking map; confirm existing hierarchy before creating any missing child record. No duplicate or prematurely In Progress issues.

Separate the recovery cases that need different decisions: a lost reply with a live owner; a verified dead owner with retained state; cancellation/hold/reassignment; expired authority; a known completed external effect with missing acknowledgement; an unknown effect; and resource retirement. Place interruption points before and after durable intent, reservation/start, provider mutation, commit/publication and reporting where their consequences matter. Combine cases when their assertions remain independently meaningful; do not multiply every case across all profiles.

Define which original identities and receipts are reused, when a fresh explicitly authorized successor is legitimate, and which uncertain/foreign situations must remain blocked. Current human decisions and scope take precedence. Preserve original grants, deadlines, keys, accounting, attempts and failures. Fresh authority must have explicit lineage; it must not rewrite or revive the predecessor. Do not claim exactly-once external execution from idempotent local records.

Use the DP04 resource-disposition table as the authority for historical retained cases. It includes DP02/DP03 protected work, DP03 v3 and DP04 v2 uncertainty, TST-323, TST-336, and decision-label candidate `10bbf8fc-e248-4699-8705-888a79f174ca`. Classify which can be supported by the proposed recovery contract and which must stay protected. Do not adopt unowned fixtures, recreate a missing journal, resend an uncertain create or remove a foreign/changed resource. Prove recovery in fresh owned fixtures before any approved historical reconciliation; do not make speculative historical cleanup the first delivery.

Select only relevant tests against requirements, affected consumers and failure risks. Reuse compatible DP04 results with their original bindings. Its complete failed run plus bounded corrections must retain that history; do not rerun thousands of unrelated cases or global static scans to plan or routinely close a small recovery change. Required genuine interruption/restart/expiry demonstrations remain necessary; mocked controls cannot replace them. Broader scheduling/performance, all-host and final-release gates remain DP06, DP07 and DP14 respectively.

Apply all thirty protections through `docs/implementation_plan.md#review-failure-safeguards-for-every-remaining-plan`. Use compact references, changed/unresolved review only, one task record and a sufficient test set fixed before expansion. Preserve independence, finding IDs, useful behavioral regressions, safeguard purposes and final-artifact checks. No extra compliance checklist, evidence database, scheduler or action diary; custom notes are lessons-only.

All human-readable application and coordinated Codex/Claude output must use current Technical Writer, not hard-coded application prose templates. If Technical Writer does not cover a needed document type/structure, tell me and ask whether to create that template before dependent authoring. Continue eligible independent work. Do not edit source skills or count recorded lessons as implemented permanent fixes.

Present the detailed plan, usable story outcomes, reuse/gap assessment and unresolved material choices for my single approval. Stop before implementation until that approval arrives. Reading this prompt, roadmap scope or a completed DP04 is not DP05 implementation approval.

### Autonomous execution after that one approval

The approved DP05 plan covers its complete scoped implementation, affected tests, fixes, independent reviews, integration, finite genuine qualification, reporting and supported resource disposition. Do not seek routine approval per task, story, retry or handoff. Required typed internal authority and current observations still apply; no silent account/model/API fallback or automatic renewal of old deadlines.

Before admission, carry the approved plan, this prompt, applicable shared instructions and tracking changes into the execution worktree. Reconcile the active-delivery decision to accepted DP04 and approved DP05 without overwriting delivered runtime/configuration or unrelated work. Inspect that transfer once; no new manifest. Do not activate an older plan's automation prompt.

Complete one usable story at a time, demonstrate its installed connections, integrate and advance. Review complete changed engineering work independently; re-review only relevant corrections or unresolved findings. Delegate only substantive outputs benefiting from parallel work, expertise or required independence, with actual ownership/ACK/START and required isolated resources. Idle capacity does not justify busywork.

After every result, settle ownership, correct the actual gap, review/integrate/close, confirm Linear updates and mark accepted plan items `DONE -` with stable IDs and dated evidence. Immediately take the next eligible task. A handoff, dispatch, status answer or report is not a stopping point. Use bounded waits while healthy work runs and change the approach after unchanged failures instead of repeating preparation/reviews.

Continue until all approved tasks, stories and epic criteria pass and owned obligations are settled or explicitly protected under the approved contract. Pause for my explicit request, material scope expansion, a needed unsupported-template decision, or exclusively external prerequisites after eligible work is exhausted. Name the exact blocker, owner and resume action; continue independent approved work where possible.

Deliver the cumulative working increment, supported recovery procedure, exact source/package, relevant test and genuine execution evidence, measured outcomes and resource dispositions, followed by the DP06 handoff. Do not implement DP06. No normal-session activation, production, default-branch merge, source-skill changes or AI Index migration is authorized. AI Index remains read-only scenario evidence with no script copies or runtime coupling.
