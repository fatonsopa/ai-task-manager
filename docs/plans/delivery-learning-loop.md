# Delivery learning loop

Status: Requested recording protocol, 13 November 2026. Planning records have started. DP01 has user-confirmed approval in its owning session; retain that approval and record current execution there. This protocol applies to every delivery plan and task. It does not activate an autonomous agent, reopen existing approvals or modify the product's authority rules.

Sources: the [delivery roadmap](incremental-delivery-proposal.md), [Policy (POL)](../policy.md), [Test Plan (TP)](../test_plan.md), existing private build ledger and the user's request for learning across plans and a future specialist-agent ecosystem.

## Purpose

Learn whether the structure **product goal → epic → delivery story → engineering task → verification → review → integration → delivery** produces working results. Retain enough evidence to understand planning mistakes, failed handoffs, implementation defects, repeated reviews, waiting and successful corrections. Apply relevant lessons before the next plan so the process can improve over time.

A log is evidence for a conclusion, not proof that a conclusion is correct. A completed task, passing test, additional reviewer or changed prompt does not itself demonstrate a better delivery method. The records must show the resulting user capability and quality as well as effort.

This is development-process learning. Product incident repair/update is separately required by R35 and remains DP13. A product defect may reveal a process problem, but the two classifications and their evidence remain distinct.

## Records and ownership

Use the existing Git, task, test, review and build records as primary evidence. Link their identities and hashes instead of copying their contents or inventing a parallel execution database.

Follow [work-item identity and traceability](../implementation_plan.md#work-item-identity-and-traceability) for all records. Preserve independent task IDs and store their current parent story separately. Keep actual Linear identifiers/links distinct from planning IDs. A parent change appends its old/new relationship, reason and authority; it does not rename the task or rewrite earlier events, reviews or evidence.

Follow [Linear delivery tracking](../implementation_plan.md#linear-delivery-tracking) for every plan, epic, story and task. Record each confirmed provider mapping and state transition with its readback, including handoffs, review, closure and recovery from a synchronization failure. An unknown Linear mapping is acceptable in a planning observation; it blocks work admission until reconciled. A supersession event links the original record, retained evidence, successor plan and reversible archival action. Tracking administration contributes no product acceptance credit.

| Record | Location relative to the repository | Purpose |
| --- | --- | --- |
| Protocol | `docs/plans/delivery-learning-loop.md` | Common recording, completion and learning rules. |
| Event journal | `.local/delivery-learning/<run-id>/events.jsonl` | Append meaningful planning and execution observations in order. |
| Lesson journal | `.local/delivery-learning/<run-id>/lessons.jsonl` | Append problem diagnosis, proposed correction, observed result and later reuse. |
| Recovery checkpoint | `.local/delivery-learning/<run-id>/checkpoint.json` | Current plan/version, event pointers, open obligations, applicable lessons and exact next action. Rebuildable from journals/evidence; not an independent source of authority. |
| Original build records | `.local/builds/20261101T155648Z/` | Preserve original attempts, accounting, failures, grants and source-bound results. |

The current planning journal is `.local/delivery-learning/20261113-planning-v1/`. It records this planning work and points to previous evidence. It does not backfill invented events or timing for earlier work. A later delivery run links the preceding checkpoint and original build identity.

The lead is the journal writer. Specialists return structured results; the lead reconciles and appends them using unique event IDs. Keep entries append-only by procedure: corrections name the earlier event and append the corrected observation. Ordinary local files are not tamper-proof. Do not claim cryptographic auditability or automatic capture before a corresponding implementation exists and is verified.

Keep directories private and journal files mode 0600. Store credentials, personal task prose and unrestricted tool output outside the journal; retain scoped private evidence references where needed. Never record secrets, hidden reasoning or speculative narratives as observations. Record concise decision rationale, relevant alternatives and observable facts. Export or publication of private records needs its applicable authority.

## What gets recorded

Record each meaningful work unit when it starts and when its outcome is known. Routine reads serving one investigation can share a record. Existing tool/test/process logs provide detail. Avoid a separate essay for every command or a second review ceremony merely to populate the journal.

| Event | Required content |
| --- | --- |
| Plan creation or revision | Plan ID/version; goal and requirement sources; inspected source/evidence; dependencies; intended outcome; inclusion/exclusion decisions; unresolved choices; who authorized a material change. |
| Work admission and start | Task/story/epic links; actor and owned worktree/resource; expected deliverable; actual acknowledgement and start; current authority, bounded invocation and check-selection basis. Queued work is not running work. |
| Action outcome | Actual command/tool or bounded activity; source/case/environment identity; expected and observed result; return/result state; produced artifacts and evidence; exact next action. Unknown effects remain unknown. |
| Verification | Task criteria assessed; linked story/epic criteria; checks selected and reason; reused evidence and binding; pass/fail/blocked/not-run results; remaining parent criteria. |
| Review | Author self-check separately from independent reviewer; reviewed candidate; findings and criteria; resolutions; accepted/changes-requested/blocked verdict and coverage limits. |
| Handoff or wait | Sending and receiving roles; input/output contract; actual receipt/acceptance; wait start/end; owner; missing input or decision; reason for rejection; next obligation. |
| Integration or delivery | Integrated source/package; actual supported behavior; story/epic closure evidence; known limits; cleanup/retained resources; user-facing report reference. |
| Problem or correction | Process failure, product defect, environment failure or requirements ambiguity; occurrence, impact, linked evidence, cause confidence, correction and verification status. |
| Plan closure and next-plan preparation | Actual outcome versus promise; timing/quality measures; lessons retained/rejected/open; lessons applied to the next plan and the corresponding changed section/check. |

Capture operational commands by reference or sanitized form. Preserve exact safe identifiers needed to correlate evidence. Requested and observed host/model identities are separate fields; an unobserved model remains unknown. Reports are facts only when actually delivered, with a delivery timestamp.

### Common event fields

Every event uses the following contract. Conditional fields can be null with a reason; do not invent values to fill the schema.

| Fields | Meaning |
| --- | --- |
| `schema_version`, `event_id`, `recorded_at`, `occurred_at` | Version, unique identity, actual recording time and actual occurrence time. Use null for unknown occurrence; imported history names its source. |
| `event_type`, `work_id`, `plan_id`, `plan_version`, `epic_id`, `story_id`, `task_id`, `attempt_id` | Hierarchy and occurrence identity. Roadmap work can precede the creation of an epic/story/task. |
| `linear_identifier`, `linear_url` | Verified provider identifier and link, separate from `task_id`; null when not recorded. Do not invent a tracker issue from a local ID. |
| `actor`, `authority` | Observed role/agent/model where known; applicable scope and decision/grant reference. Logs cannot grant permissions. |
| `source_refs`, `case_refs`, `environment_ref` | Exact candidate, changed files/interfaces, checks and environment needed to judge evidence reuse or compare failures. |
| `action`, `expected`, `observed`, `outcome` | What was done, intended result and factual outcome. Distinguish planned, running, passed, failed, blocked, canceled and unknown. |
| `criteria` | Task verdict and each affected parent criterion, evidence and remaining obligations. |
| `evidence_refs`, `review_ref`, `handoff_ref` | Primary records, content hashes where available and role connections. |
| `measurements` | Measured planning/implementation/verification/review/integration/waiting durations and observed usage where available; unknown remains null. |
| `product_advance`, `acceptance_advance` | Whether the event establishes delivered implementation or a required acceptance result, with evidence. Writing and setup alone do not set these true. |
| `next_action`, `supersedes` | One responsible owner/next action; optional reference to a corrected earlier event. |

For a task event, `task_id` is the independent work identity and `story_id` records its explicit parent at the time of the event; never derive one from the other. For task completion, `criteria` records the shared Definition of Done version, task acceptance verdicts and the IDs of relevant story and epic criteria. Include the owning plan for an otherwise ambiguous criterion ID and link each assessed criterion to its tests/cases, candidate-bound evidence and verdict. Each parent disposition is `satisfied_by_this_evidence`, `contributes_but_parent_pending`, `blocked` or `not_affected`, with evidence or rationale. Missing criteria mapping prevents admission; missing closure evidence prevents completion. Several tasks may contribute evidence to one criterion without duplicating the criterion or completing the story early.

## Task, story and epic completion

Before implementation, define a shared quality Definition of Done and item-specific acceptance criteria. Trace task criteria to the story and epic outcomes. An internal task still needs a real consuming story or explicitly required assurance outcome.

1. The author checks the complete assigned change and relevant regressions against its task criteria and affected story/epic criteria. A small task can contribute to a larger criterion without satisfying it alone.
2. Independent review checks the exact candidate, material findings and evidence against the same criteria. Self-review is identified as self-review. Review acceptance is not integration or product acceptance.
3. Integration confirms the changed producer/consumer interfaces and affected existing behavior. Reuse prior checks only with valid bindings; changes can invalidate them.
4. Story closure verifies the full promised behavior on the integrated artifact. Required real host/provider/user interactions remain real; fixtures cannot replace them.
5. Epic/plan closure verifies all required stories, their interactions and the overall promised outcome. A sum of task passes cannot replace this result.

Task-level checks consider parent outcomes through impact and traceability. The complete epic suite is required at epic closure and earlier when mandated by risk or changed behavior. Blindly rerunning it after every task increases feedback time without establishing additional evidence for unchanged behavior.

## Specialist handoffs for the future ecosystem

The records must be usable by a future main agent and product-design, engineering, quality, security, DevOps and review specialists. These roles are prospective; recording a role does not mean an agent was launched or that every task needs every specialist.

Every actual handoff records:

- The sender, intended receiver, actual receiver and current accountable lead.
- Independent task ID, explicit parent story/epic links, supported criteria, actual Linear identifier/link when known, exact artifact/version and the problem or decision requested.
- Inputs, owned scope, interface assumptions and expected output.
- Applicable Definition of Done, acceptance criteria, required evidence, authority and exclusions.
- Actual acknowledgement, start, completion and acceptance/rejection, including timestamps and reasons.
- Findings, unresolved decisions, retained resource/effect obligations and the next owner/action.

The receiver's assessment of input sufficiency helps locate bad delegation. The receiver's output and the next consumer's acceptance help locate incomplete work or ambiguous contracts. The lead records whether a specialist was needed, and why, using affected behavior and risk. There is no mandatory design/security/DevOps loop for every small change.

Before a future resume, read the agreed plan/version, relevant verified lessons and checkpoint; then reconcile actual processes, resources, Git and effects with the primary records. Choose the next eligible task only after predecessor obligations are settled. Never use an old log, generated plan or imported lesson as new authorization.

## Improvement loop

Use **observe → diagnose → change within authority → compare → retain, revise or reject**. [Kanban's workflow-improvement practice](https://kanbanguides.org/the-kanban-guide/2025.5/) supports inspecting flow and adapting it. The concrete records and triggers here are project choices.

| Step | Required result |
| --- | --- |
| Observe | Link a real failure, blockage, confusing plan, review recurrence, escaped defect or successful outcome to its events and criterion. |
| Diagnose | Separate symptom from cause. State confirmed facts, hypotheses, scope and competing explanations. A product bug does not automatically prove a decomposition or review failure. |
| Propose | Specify one actionable correction, intended effect, affected workflow rule, verification measure and rollback/rejection condition. Check existing lessons before proposing another framework. |
| Apply | Record the actual version change and authority. Routine corrections inside approved work need no new permission queue. Scope, permission or acceptance changes need their required agreement. Method trials remain deferred until the user agrees the strategy and delivery plan. |
| Compare | Compare actual later outcomes with the baseline under declared workload/source/environment limits. Record benefit, harm, no effect or insufficient evidence; retain failed results. |
| Retain or reject | Promote a lesson only to the scope supported by the evidence. At the next plan, record where it was applied and whether the failure recurred. Revoke or narrow a lesson contradicted by later results. |

Each lesson revision contains a stable `lesson_id`, linked events, problem category, expected/observed behavior, impact, cause and confidence, proposed correction, authority, applied version, baseline, validation method, observed outcome, applicability, next-plan use and status. Useful statuses are `observed`, `hypothesis`, `correction_proposed`, `corrected_unvalidated`, `supported_in_scope`, `rejected` and `superseded`. Even a supported lesson has limits; one successful run does not prove a universal rule.

At plan creation, retrieve relevant lessons by problem/domain/interface/role, record whether each is applied or inapplicable with a reason, and add the applicable check or handoff rule to the plan. At plan closure, assess those lessons against actual results and record lessons for the next plan. This makes reuse observable rather than a promise that the agent will remember.

## How we judge whether the structure works

| Question | Evidence and measure |
| --- | --- |
| Are tasks producing usable stories and epics? | Completed tasks linked to actually accepted parent outcomes; completed tasks whose parents remain blocked; age and exact missing parent criteria. Report numerator and denominator, not activity counts alone. |
| Are plans understandable and executable? | Missing acceptance/dependency discoveries after admission, rejected handoffs and clarification/replanning events. Distinguish legitimate discovery from repeated known omissions. |
| Does review prevent defects without trapping work? | Material findings by stage, findings reopened after claimed fixes, post-completion defects, review turnaround and repeated unchanged reviews. A high finding count alone is neither success nor failure. |
| Is delivery becoming faster? | Comparable task/story/plan cycle time, work-item age, throughput, implementation/review/test time and waiting by cause/owner. Keep scope and workload differences visible. |
| Is rework falling? | Work repeated because an acceptance criterion, interface, authority or prior lesson was missed, with linked cause and actual effort when measured. |
| Do handoffs work? | Complete accepted inputs/outputs, time waiting for the lead or another role, missing ACK/START, rejected outputs and unclaimed obligations. |
| Are lessons preventing recurrence? | Applicable later plans, recorded use of the lesson, observed recurrence or absence under the relevant conditions, and evidence limits. |
| Are quality and authority preserved? | Relevant regression/real acceptance results, ownership/isolation violations, premature closure, unknown effects and unauthorized actions. |
| Is logging itself becoming overhead? | Measured recording/analysis effort where available and whether a log/review duplicates an existing record without changing a decision. |

Use consistent start/end definitions. Record task/story/plan elapsed time separately from summed worker effort; parallel work overlaps. Do not calculate active effort by subtracting guessed wait from wall time. Usage/cost metrics require actual counters. Missing data is unknown, not zero. Compare similar work and disclose major confounders before crediting the methodology.

The 20-minute report, once reconfigured after agreement, should summarize these observations beside delivery progress. One interval without verified implementation or required acceptance progress is at risk; two such intervals, or two failures with unchanged candidate/case/environment, require diagnosis and a changed action. Healthy bounded long-running checks are inspected rather than restarted merely because the timer fired. Every root-caused wait gets an owner and next action.

Reuse the existing progress guard and primary ledger after their scheduling contract has been reconciled with the agreed roadmap. Do not build a second controller or rename unchanged retries to evade it. Keep retrospectives focused on the current blocker and the next plan; avoid turning improvement analysis into another parallel project.

## Reuse for a future autonomous agent

The retained material can later supply realistic planning/execution examples, failure cases and evaluation criteria. An episode should include the information actually available at the decision point, the proposed action and authority, observed tool/handoff results, closure verdict, relevant lesson and later outcome. Keep unsuccessful and uncertain episodes as well as successes.

Before using logs to evaluate a future agent, remove private content, preserve source provenance, define independent success criteria and separate examples used for instruction from held-out evaluation cases. The agent's own positive verdict cannot be the only quality measure. Replaying a trace is not fresh live acceptance.

A future controller can automate event capture, criterion checks, lesson retrieval, role routing and resume decisions over these contracts. That controller and its capability/authority tests remain separate future work. The present journal is written by the lead; it does not automatically intercept tools, enforce immutable logging or guarantee unattended completion.
