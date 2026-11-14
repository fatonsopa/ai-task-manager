# AI Task Manager delivery roadmap

Status: Master Implementation Plan (IP) proposal, revised 13 November 2026. The user confirms that DP01 is already approved in its owning session. Preserve that approval and its scope; later detailed plans require their own agreement. These shared planning rules do not reopen an approved plan or grant additional execution authority.

Sources: the approved [Implementation Plan (IP)](../implementation_plan.md), [Spec Driven Development document (SDD)](../spec_driven_development.md), [High Level Document (HLD)](../high_level_document.md), [Policy (POL)](../policy.md), [Test Plan (TP)](../test_plan.md), [requirement inventory](../../config/requirements.json) and [55-item delivery readiness audit](../audits/delivery-readiness-2026-11-13.md).

## Goal and specification

Deliver the approved product through the numbered plans below. Each plan adds a usable capability to the same integrated product. Reuse existing implementation and valid evidence, complete the selected plan, and then develop and execute the next plan.

This document is the complete proposed **delivery roadmap**: it lists the plans to develop, their order, scope, dependencies and completion results. A **detailed delivery plan** explains how to implement one row. DP01 is the first such plan to develop. Its title must remain identical in the roadmap, detailed document, tracker, logs and reports. The approved DP01 detailed plan is maintained at `docs/plans/deliveries/DP01.md`. Detailed DP02–DP14 plans remain future work.

The roadmap contains plans, not a backlog of user stories. Stories and engineering tasks are developed inside the selected plan after the roadmap is agreed. The original P00–P10 identifiers remain requirement and acceptance references. Changing the schedule cannot change their obligations or turn partial evidence into a pass.

## Current context and approach

The source audit examined main at `0285a8fda6067210c687d35b019a235fbaf30676`. P00–P02 have retained acceptance, and later phases contain substantial implementation, incomplete connections, missing capabilities and interrupted candidates. The audit did not execute product tests. Existing work is the starting point for every plan.

Use **incremental delivery with Kanban flow, small integrated changes and continuous integration**. Use rolling-wave planning: define the full sequence now and detail only the next ready plan. This avoids committing to distant implementation details before earlier deliveries establish their interfaces.

The practices are established; the plan titles, sequence and limits are proposed project choices:

- [DORA: small batches](https://dora.dev/capabilities/working-in-small-batches/) supports small, verifiable changes and short feedback cycles. A plan may contain several small deliveries; it must not become one large branch awaiting integration.
- [The Kanban Guide](https://kanbanguides.org/the-kanban-guide/2025.5/) covers explicit workflow, work-in-progress control and improvement using flow measurements.
- [The Scrum Guide](https://scrumguides.org/scrum-guide.html#increment) defines usable, cumulative increments and a shared Definition of Done. This proposal does not claim to implement the complete Scrum framework.
- [DORA: continuous integration](https://dora.dev/capabilities/continuous-integration/) supports frequent integration and prompt feedback on changes.
- [OpenAI: long-horizon tasks](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex) and [Anthropic: long-running agent harnesses](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents) support explicit work boundaries, persistent context and verification of completed behavior.

These sources do not guarantee autonomous delivery or prescribe this particular plan catalogue. The method's effectiveness must be measured after agreement and real execution.

## Work sequence

Develop and deliver DP01, then DP02, continuing in numerical order through DP14. Each plan inherits the working product from the preceding plan. The dependency column identifies important technical inputs as well as that cumulative sequence. Earlier plans include the setup, safety, recovery and host support necessary for their own promise; later plans extend those capabilities. A known defect that invalidates the current promise must be resolved in the current plan.

### Complete proposed plan catalogue

DP01 is delivered on 14 November 2026; see its [delivery handoff](deliveries/DP01-handoff.md). DP02–DP14 remain roadmap items awaiting their detailed plans and agreement. A working result describes the intended delivery, not an observed pass. Every plan is tracked in the [TST development project](linear-tracking.md); current state is confirmed from the owning session and Linear.

| Order and exact plan title | Scope of the plan | Working result required for completion | Important inputs |
| --- | --- | --- | --- |
| **DP01 — Install and prepare approved tasks** | Supported isolated candidate installation and trusted setup; exact approved new-task intake; explicit queueing and readback. | An operator starts in an empty project, creates one real scoped Linear task in Backlog, explicitly queues it to Todo and sees confirmed matching state through the installed product. Required refusals, replay and owned cleanup work. | Accepted Core and initial preparation code; existing wheel and trusted setup API. |
| **DP02 — Run and report one non-code task** | One genuine CLI worker, task-owned environment, supervision, independent review and the complete lead/result receipt chain. | One queued non-code task runs through an actual supported CLI, completes its reviewed result and permitted terminal transition, confirms CLI and Linear state, and reaches the lead's acknowledgement and user report. | DP01; existing workflow, host, review and lead-reporting components. |
| **DP03 — Review, commit and merge one code task** | Worker-owned code changes and commits; independent candidate review; product-owned merge and delivery reporting. | One code task produces a reviewed commit, merges through the product into the trusted target, reaches verified In Dev, reports the result and settles owned resource obligations. | DP02; standalone merge domains and retained scenario analysis. |
| **DP04 — Synchronize task changes and human decisions** | Existing-issue intake; immediate outbound and authenticated inbound synchronization; polling recovery; holds, assignments, dynamic agent delegation, decisions and lifecycle conflicts. | Real scoped human and agent changes reach the installed product and Linear with authoritative readback, deduplication, correct decision-only labels/mentions and two-user isolation. | DP01–DP03; existing Linear adapter, listener and reconciliation components. |
| **DP05 — Recover interrupted and expired work** | Owner/process restart, crashes, cancellation, partial effects, uncertain writes, retained resources and explicitly authorized successor handling. | An interrupted supported task resumes or stops through the declared recovery interface without duplicate work, invented authority, lost changes or removal of foreign resources. | DP02–DP04; retained recovery candidates require current review and reconciliation. |
| **DP06 — Schedule and deliver concurrent tasks** | Dependency/priority scheduling, starvation visibility, finite capacity, declared conflicts and concurrent merge/resource performance. | Eligible independent tasks run safely in parallel and deliver through one merge authority. Dependency and capacity decisions are truthful; frozen small-change and concurrent performance criteria pass. | DP03–DP05; atomic Core admission, queue and resource contracts. |
| **DP07 — Run complete workflows in Claude Code and Codex CLI** | Complete managed-host integration, equivalent enforcement, actual profile/account/effort provenance and worker-to-lead delivery in both hosts. | Both installed hosts perform the supported workflows under their required actual model profiles; unmanaged paths are refused and result reporting is observed. | DP02–DP06; minimum genuine host support is already required by earlier plans. |
| **DP08 — Use the task sidebar in supported terminals** | Session-scoped task projection, approved sidebar interaction, real host layout, accessibility and reconnect behavior. | A user can inspect and interact with the same live task system in the declared terminal layout without stale or duplicate actions or interference with host-native agent space. | DP04 and DP07; existing terminal/view code and retained PTY evidence. |
| **DP09 — Install and operate the coordination skills** | Finish the required focused skill artifacts, generated guidance, actual discovery and fresh behavior evaluations. | Both isolated hosts discover the packaged skills and follow the supported coordination workflow without private checkout assembly or duplicated policy. | DP07–DP08; reuse existing skill candidates and their failures. |
| **DP10 — Update, disable and remove the product safely** | Complete host packages and supported setup/diagnostics; update, disable, uninstall, state export and retained configuration/history. | An operator installs and manages the product lifecycle in an empty or unrelated project, retaining authoritative history and foreign settings and recovering from an interrupted operation. | DP07–DP09; DP01 supplies the initial candidate installation. |
| **DP11 — Migrate projects with one task-state writer** | Generic inventory, required state transfer, obsolete-writer fencing, interruption and rollback/reconciliation. | A declared independent migration fixture cuts over once and preserves task/decision/attempt/resource/pending-sync history, including recovery after new writes. | DP04–DP05 and DP10; target-specific AI Index work remains deferred. |
| **DP12 — Deliver push, release, hotfix and target synchronization** | Product-owned contracts for the separately required `#push`, `#release`, `#hotfix` and `/sync-with-dev` purposes, with destination-specific evidence. | Each required delivery purpose works in isolated fixtures with correct status/reporting and recovery, using the new interfaces and one merge authority. | DP03, DP05 and DP10; production effects remain outside authority. |
| **DP13 — Repair opted-in product incidents** | Private incident/feedback capture, redaction, bounded authorized repair, regression evidence, verified update/rollback and recurrence checks. | An opted-in product incident can become a reproduced defect, reviewed repair and verified update with safe recovery and retained failures. | DP05 and DP10–DP12; this is product requirement R35, distinct from the development-process learning log. |
| **DP14 — Qualify and deliver the complete release** | Final combined suite, every required live Linear/profile/platform/endurance/cutover case, reproducibility and release evidence. | The exact cumulative source and packages satisfy every original release criterion, with supported operating/recovery guidance and a truthful delivery record. Publication and normal activation require their applicable authority. | DP01–DP13; earlier matching results are reused only where the final contract permits. |

After DP14, two separately gated items remain visible: the fresh target-specific AI Index audit/removal/adaptation requested only after standalone completion; and the deferred Engineering Lead agent/coordination-skill organization. Their later execution requires the applicable user request. They are not prerequisites for claiming the standalone product complete, nor permission to modify the current target or build an agent ecosystem now.

### DP01 — Install and prepare approved tasks

This is the first row above and the first detailed plan to develop. It adds a supported operator path around existing implementation rather than rebuilding Core.

**Included:** a packaged isolated candidate; supported trusted configuration/enrollment; approved task prose; real Backlog creation; explicit Todo enqueue; installed list/show; exact scoped readback; replay/refusal behavior and owned cleanup.

**Existing boundary:** `aitm --config CONFIG workflow prepare --input REQUEST` permits `writing_prepare`, `writing_review`, `writing_finalize`, `task_submit`, `task_show`, `task_list` and `task_action` with `action: "enqueue"` only before first owner activation. Trusted setup currently exists as `installation.sessions.setup`; a public setup command has not been established. The detailed plan must define its supported interface before implementation.

Preserve the private sealed configuration, signed provider/operator authority, finite capacity, expected human/agent identity, activation latch and absence of execution history or unsettled sends. Preparation is a limited initial operation; ordinary runtime commands continue to require their configured owner. Existing-issue find/inspect/admit belongs to DP04, and genuine worker execution begins with DP02.

**Completion result:** perform the complete DP01 row through the installed artifact without editing databases, injecting test-only authority or relying on an undocumented private helper. The result record must identify exact source/package, expected identities and prose, one issue, confirmed Backlog/Todo, duplicate/scope refusals and cleanup. This proposal has not executed those checks.

**Next planning output after agreement:** author `docs/plans/deliveries/DP01.md` with verified current context, interfaces, dependencies, existing evidence, missing implementation, stories/tasks, completion criteria, affected checks, operator procedure and recovery. No story or task backlog is created at this roadmap stage.

## Work structure inside a detailed plan

Retain **product goal → epic → delivery story → engineering task**. Modules/domains identify architecture and ownership; they are not an additional mandatory planning level. The roadmap orders delivery plans. A detailed plan is the execution document for a bounded outcome epic, not another issue hierarchy. A small epic may have one story.

Apply [work-item identity and traceability](../implementation_plan.md#work-item-identity-and-traceability) to every plan. Task IDs remain independent of story membership: retain `DP01-T01` through `DP01-T08`, record each parent story explicitly and link acceptance criteria separately. Future task registers use Task ID, Title, Parent story, Criteria supported and the actual Linear identifier/link when known. Reparenting changes the relationship, not the task ID or its history.

Apply [Linear delivery tracking](../implementation_plan.md#linear-delivery-tracking) before admitting work. All plans, epics, stories and tasks must be represented in TST, with confirmed native parent relationships and actual identifier links. Create detailed child work only when it has been defined; roadmap visibility is not execution approval. Update records on each handoff and acceptance, preserving all reusable work when retiring an obsolete schedule.

| Level | What it owns | Evidence needed to close it |
| --- | --- | --- |
| Product goal | Complete agreed product, constraints and release criteria. | All required outcomes and final qualification, with no required failed, blocked or unrun case hidden. |
| Epic | The bounded outcome of one delivery plan. | All required story outcomes plus the integrated epic result and relevant operational/quality criteria. |
| Delivery story | A usable addition to the current product. | Its full acceptance behavior through real producer/consumer connections on the integrated artifact. |
| Engineering task | One owned contribution to a story. | Its exact deliverable, its acceptance criteria, applicable shared quality criteria, affected parent-level checks and independent review where required. |

Stories are integrated and demonstrated as they finish. Completing the entire plan must not delay delivery of a usable story. An enabling task may build an internal interface, but it must name the story and epic outcome that consumes it. Research or document completion alone does not count as shipped product behavior.

The approach also scales to a larger system. An operating-system roadmap could order plans for boot/diagnostics, running a program, persistent storage, isolated concurrent programs, networking/interaction and installation/update. Each adds to one runnable image. A boot diagnostic can be a useful developer delivery without satisfying the complete consumer operating-system goal. This is an illustration, not an audited design or executed method trial. [SEBoK](https://sebokwiki.org/wiki/Incremental_Development_Approach) provides broader systems-engineering context.

## Definition of done and parent verification

Use one shared Definition of Done for quality, plus item-specific acceptance criteria at task, story and epic levels. Stable criteria IDs connect the levels. These are completion contracts for this project; the three-level naming scheme is not prescribed by the Scrum Guide.

| Checkpoint | Required assessment | What remains open |
| --- | --- | --- |
| Before task admission | Trace task acceptance and affected checks to exact story and epic criteria. Identify ownership, interfaces, authority, existing evidence and a verifiable result. | Every unimplemented or unverified criterion. |
| Task completion | Prove the assigned deliverable and common quality criteria. Run or reuse valid affected story/epic checks. Explain how the task advances each referenced parent criterion. | Parent criteria requiring other tasks or integrated demonstrations remain pending. |
| Story completion | Exercise the complete story on integrated source/artifact, including its incoming/outgoing connections, refusals, recovery and relevant real-host/provider evidence. | Other stories and epic-wide criteria. |
| Epic/plan completion | Verify the cumulative outcome, cross-story interactions, all required story results and epic-wide criteria. Supply a runnable artifact/procedure and settled ownership. | Product-wide qualification and explicitly later capabilities. |
| Product completion | Satisfy every original requirement and release check on the exact final candidate as required by the Test Plan (TP). | Separately authorized activation or target migration may still be pending. |

A task record must contain its own verdict and a disposition for every linked story/epic criterion: `satisfied_by_this_evidence`, `contributes_but_parent_pending`, `blocked` or `not_affected`, with an evidence reference or reason. Completing all task checkboxes is insufficient to close a story or epic without its integrated outcome.

Check selection follows impact and required acceptance. A change that affects a shared contract runs the relevant parent-level regression checks immediately. Reuse an unchanged accepted result only with matching source/dependency/configuration/case bindings. Full parent suites run at their required closure points and earlier when the change warrants them; they are not blindly repeated for every small task.

Shared completion criteria require implemented behavior, preserved scope/security/ownership, appropriate passing checks, resolved material review findings, integration, current instructions and exact evidence. Self-checking precedes independent review of complete changes. A reviewer checks findings against the relevant task/story/epic criteria; the author cannot turn a self-review into independent acceptance.

## Requirement coverage

All 38 requirements remain in scope. The plans identify implementation responsibility; the final release gate retains complete acceptance. Multiple plans can contribute to one requirement without counting it twice.

| Plan | Requirement coverage |
| --- | --- |
| DP01 | R02, R04, R10–R12, R15, R23–R24, R27, R38 |
| DP02 | R01, R03–R06, R09, R13–R16, R19–R20, R26–R27, R30–R31, R36 |
| DP03 | R05, R08–R10, R14–R15, R25, R30–R32, R36 |
| DP04 | R02, R04, R07, R09–R13, R23, R27, R30, R36–R38 |
| DP05 | R03, R05, R10, R12, R14–R15, R19–R20, R26–R27, R30–R31, R36–R37 |
| DP06 | R05–R08, R11, R14–R15, R19–R20, R25, R27, R30–R31 |
| DP07 | R01, R03–R04, R15–R16, R21–R22, R26–R27, R36 |
| DP08 | R04, R15, R17, R24, R27, R36 |
| DP09 | R01, R15–R16, R21–R22, R26, R33, R36, R38 |
| DP10 | R18, R24–R25, R27, R31, R33–R34 |
| DP11 | R18, R24, R27–R28, R30–R31, R33–R34 |
| DP12 | R08–R09, R13–R15, R25, R30–R32, R36 |
| DP13 | R18, R20, R25, R27, R31, R35 |
| DP14 | R01–R38; all original release obligations, with later target-specific authority retained. |

The exact 55-item audit remains the source inventory. This mapping shows where every phase item contributes; it is not a new acceptance score or a claim of phase completion.

| Original items | Proposed delivery home and retained obligation |
| --- | --- |
| P00.1–P00.5; P01.1–P01.5 | Reuse accepted preparation. Before authorized execution, reconcile current reality and update only invalidated prerequisites/contracts; preserve old records. |
| P02.1–P02.5 | Reuse accepted Core across DP01–DP06; requalify affected changes. |
| P03.1 | DP02 establishes a genuine owned worker; DP07 completes both-host coverage. |
| P03.2 | DP02/DP03 include necessary allocation/cleanup; DP05/DP06 complete recovery and resource contention. |
| P03.3–P03.4 | DP02 establishes the supported run; DP05 completes supervision and resume/fault obligations. |
| P03.5 | DP03 delivers review, commit and merge. |
| P03.6 | DP03 includes correct single-task checks/lifetime; DP06 completes concurrent recovery, stage timing and frozen performance cases. |
| P03.7 | DP03 proves one genuine code path; DP07 completes host coverage. |
| P04.1–P04.2 | DP01 supplies initial intake; DP04 completes existing intake and synchronization cases. |
| P04.3–P04.5 | DP04 delivers full scoped inbound/human/lifecycle behavior, with earlier required safety retained. |
| P04.6 | DP04 demonstrates real two-user isolation and cleanup; DP14 closes all remaining live acceptance, including endurance. |
| P05.1–P05.5 | DP02/DP03 include actual first-host execution and reporting; DP07 completes installed host/profile/managed-surface coverage. DP09 adds complete skill discovery. |
| P06.1–P06.2 | DP06 delivers complete scheduling/capacity/conflict behavior. |
| P06.3–P06.4 | DP05 completes interruption and authority recovery. |
| P07.1–P07.4 | DP08 delivers terminal behavior and real PTY/host composition. |
| P08.1–P08.4 | DP09 completes artifacts, generated guidance, fresh behavior and installed discovery; minimum instructions needed earlier remain part of their delivery. |
| P09.1 | DP01 starts isolated candidate installation; DP10 completes supported self-contained host packages/lifecycle. |
| P09.2 | DP10 delivers lifecycle operations. |
| P09.3–P09.4 | DP11 delivers generic cutover/recovery; the original target stays read-only. |
| P09.5 | DP10/DP11 supply complete generic distribution/guidance; DP14 verifies final artifacts. Target-specific guidance awaits the later request. |
| P10.1–P10.5 | DP14 closes final combined, live/profile/platform/endurance and delivery evidence. |

## Execution and learning

The [delivery learning loop](delivery-learning-loop.md) defines the records used while planning and executing. Log actual actions, outcomes, review findings, blocked handoffs and corrections. Before detailing each next plan, use relevant verified lessons to update that plan's assumptions, decomposition, checks and handoffs. Record what was applied so the next outcome can confirm or challenge the lesson.

One plan outcome is active at a time. Within it, prioritize finishing the current usable story. Independent tasks or selective specialist reviews may run in parallel when interfaces and ownership are settled and actual capacity permits. Do not add unrelated work to occupy a slot or route every change through every specialist.

The lead owns plan selection, evidence-based closure and the next handoff. Workers own bounded tasks in separate worktrees and declared disposable databases. Scripts enforce deterministic permissions, transitions and sequencing; agents implement and judge meaning. A new agent ecosystem is future work, not a consequence of creating logs.

## Reporting and method evaluation

After agreement, the 20-minute report should show every numbered plan, the active plan's exact deliverables, previous/current completion, concrete completed behavior, elapsed/waiting time, blockers/owners and the next result. Show task, story, epic and full-release evidence separately within the report. A closed task is not another delivered epic.

Group task rows beneath their stories and include **Parent story** and **Criteria supported** beside the independent **Task ID** and descriptive title. Include the actual Linear identifier/link when recorded. Criterion-linked tests and evidence determine acceptance; neither a task-ID prefix nor its displayed grouping implies completion.

Use the agreed deliverable inventory for percentages and disclose denominator changes. The historical 5/39 count is a verified lower bound; it does not show that only 13% of the implementation exists. Plans have different sizes, so `completed plans / 14` measures roadmap closure, not effort or total implementation. Until detailed inventories are reconciled, report unknown work honestly.

Evaluate whether the method converts completed tasks into accepted stories and epics, preserves quality, reduces waiting/rework and avoids repeated mistakes. The learning protocol defines the measurements and causal limits. A changed document or a passing writing check cannot demonstrate improved delivery performance.

## Rollout and recovery

Use each delivery plan's existing approval; obtain agreement when it is missing or a material change requires it. DP01's approval is already confirmed by the user in its owning session. Before execution, reconcile expired invocations, actual workers/resources, retained branches and current authority; update the maintained schedule and relevant controls consistently. Existing source/evidence and original deadlines remain preserved. An unfinished candidate or old pass cannot supply new execution authority.

Usable intermediate deliveries are isolated developer previews with declared capability limits. Normal-session activation, production changes and target-specific AI Index work remain outside current authority. Generic migration remains in DP11. AI Index is read-only scenario evidence, with no product-side script copies, aliases, imports or runtime bridge.

Keep accepted earlier deliveries usable if a new candidate fails. Preserve failures, uncertain effects and ownership. Repair or revert only the new owned changes without erasing valid later history. Full product acceptance still requires genuine host/model, live Linear, Linux, endurance, cutover and final combined evidence.

## Risks and open decisions

Future detailed plans must be developed and agreed before implementation. Scope discoveries can require a versioned plan change; a reviewer or log entry cannot silently split away an unmet requirement. DP01's approved scope and execution remain with its owning session. Clarifying task identity, parent relationships and criterion links does not change that scope.

The audit establishes reusable source and historical evidence, not current provider/model availability or a delivery-rate forecast. The logging files are a human-readable and machine-readable record, not an installed autonomous controller. Use the owning session's current execution records for runtime status; this shared document does not establish that work is running, paused or complete.
