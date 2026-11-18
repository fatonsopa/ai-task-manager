# DP06 planning and execution prompt

Status: Planning handoff, 17 November 2026. DP05 records accepted completion. DP06 requires its own detailed plan and one approval before implementation. This prompt does not supply that implementation approval.

## Prompt

Work in the AI Task Manager repository. Prepare **DP06 — Schedule and deliver concurrent tasks**, using accepted DP05 as the cumulative baseline. After I approve the detailed plan, execute its complete scope autonomously under the rules below.

Use the shared `docs/plans/delivery-session-prompt.md` as the standard method, with DP06's exact scope and accepted DP05 baseline supplied here. Include its startup-chain, smallest-usable-path and forced approach-change rules in the detailed DP06 plan alongside the verbatim execution paragraphs and completion-based reporting below. For relevant startup work, first prove sealed role → advertised tools → capture → cold verification → normal startup, then lead queue read → eligible selection → operator worker start. Reuse compatible evidence for the regression reported already added and passing; do not claim a new test or expand into unrelated checks. Fifteen minutes without delivered work or decisive evidence forces a concrete change; more preparation, component passes and review paperwork cannot justify the same approach.

### Goal and baseline

Eligible independent tasks run safely in parallel and deliver through one merge authority. Dependency, priority, fairness, starvation, queue/capacity and conflict decisions must reflect actual state. Required small-change and concurrent merge/resource performance criteria must pass with measured evidence, while preserving isolation, human decisions and recovery.

Start from `delivery/dp05`, observed at `be648a1fd5199a598152f84641fc489dec067d4e`, reconciling legitimate later changes. Read its canonical `docs/plans/deliveries/DP05-handoff.md` with `git show` if needed. Its accepted plan records 10/10 tasks, 4/4 stories and 6/6 epic criteria. Preserve exact source/package/environment bindings and all retained failures and protected resources. Accepted evidence is not reusable live authority; DP05 timings are not DP06 performance acceptance.

Read `AGENTS.md`, `docs/readme.md`, the DP06 roadmap row and relevant sections of the governing High Level Document (HLD), Spec Driven Development document (SDD), Implementation Plan (IP), Policy (POL), Test Plan (TP), Test Status Report (TSR), requirement inventory and acceptance cases. Use existing locations and bounded reads; do not reload full histories. Reconcile actual workers/processes, tasks, Git, installed controls and effects before admission. Reuse registered healthy assignments and preserve unrelated edits.

### Audit and write the detailed plan

Audit existing scheduling/admission/resource/merge behavior against DP06 requirements, distinguishing implemented and verified work from missing connections, defects and unassessed behavior. Starting points include `src/ai_task_manager/application/scheduling.py`, `domains/scheduling/`, `storage/scheduling.py`, `storage/capacity.py`, `config/capacity.py`, and the existing `tests/scheduling/`, `tests/merge_workloads/` and resource controls. These paths were located on the delivered branch; their presence is not a behavioral pass. Trace actual consumers and required source/environment bindings before reusing conclusions.

Write `docs/plans/deliveries/DP06.md` through current Technical Writer Implementation Plan (IP) guidance. Define one bounded outcome epic and dependency-ordered usable stories/tasks based on actual gaps, not an invented fixed count. Each story must add demonstrated behavior to the same installed product and integrate before the next dependent story.

Cover dependency/priority eligibility, starvation visibility/fair progress, finite capacity, declared conflicts, independent worktree/database ownership, concurrent execution and one product-owned merge authority. Define truthful blocked/waiting/running states, actual worker starts/stops, recovery after contention/failure and preserved human holds/reassignment. Capacity is an upper bound, not an instruction to launch duplicates, blocked work or unnecessary agents. Use actual host capacity, not a historical assumed number.

Map existing requirements and cases to every task/story/epic criterion and Definition of Done. Keep task IDs independent of parent-story membership; show parent stories and supported criteria separately. Reuse the DP06 plan record TST-244 and the current tracking map. Confirm existing records before creating missing children; do not mark work running without a real worker.

Freeze the smallest sufficient affected and genuine integrated verification set before expansion. Reuse verified commands with their exact required interpreter, installation, dependencies, working directory and environment. Preserve compatible historical results with their original scope; do not turn a changed report date into a rerun requirement. Do not run product tests, models or provider effects merely to prepare this plan.

Derive performance budgets and workload definitions from the governing contracts. Use representative small-change, broad-impact and concurrent workloads; distinguish queue/lock waits, checks, database/resource setup, commit, candidate/review, publication and cleanup. Report product-controlled, model and full wall-clock timing honestly, including applicable cold/warm costs. Do not infer speed from line count or label combined timing as isolated stage cost. Synthetic controls do not establish genuine concurrent delivery. Missing container/terminal/account prerequisites remain explicit; do not substitute an unverified environment or weaken a budget. Present a material ambiguity for agreement rather than inventing a threshold or benchmark sweep.

Apply all thirty protections and the four execution gates from `docs/implementation_plan.md`: compact references, changed/unresolved review only, one task record and sufficient tests selected before expansion. Preserve independence, finding continuity, relevant regressions, safeguard purposes and final-artifact verification. Keep custom notes lessons-only. Do not create another scheduler, compliance checklist, evidence database or process diary to manage development.

Identify the workflow required for each deliverable. Application engineering uses the product implementation/review process. Integration with an existing skill verifies its version/interface and relevant behavior. Creating, auditing or changing a reusable skill or agent package must invoke current Skill Builder and follow its actual workflow, with Technical Writer for its documents. Do not postpone applicable skill work to DP09, and do not rebuild accepted application code simply to rerun it through Builder. Keep source-skill modifications outside this scope unless separately authorized.

All human-readable application and coordinated Codex/Claude output goes through current Technical Writer; no separate hard-coded application prose templates. If a required document type/structure is not covered, explain the gap and ask whether to create its template before dependent authoring. Continue eligible independent work.

Present the detailed plan, usable story outcomes, reuse/gap assessment, fixed relevant verification and material open choices for my one approval. Stop before implementation until I approve. Existing DP05 acceptance and this prompt do not approve an unwritten DP06 plan.

### Required execution rules

Copy the following three paragraphs verbatim into the final detailed DP06 plan. Preserve their wording. They apply to execution after approval; planning progress must be reported as planning or decisive evidence, not invented product delivery.

Execute the approved scope through working delivery. Reuse verified commands, their required environment and valid evidence. Every additional test or review must resolve a named acceptance criterion, changed input or observed failure. Preserve required safeguards; omit optional refactors and duplicate bookkeeping.

Complete and integrate each usable story, then continue without routine reapproval. Report accepted tasks, stories and epic criteria with denominators and percentages. Never call partial work complete.

If 15 minutes pass without delivered work or decisive evidence, explain the cause and change approach. Update me at least every 60 seconds during active work. Stop only at completion or an agreed stop condition; identify any external prerequisite and exact resume action.

Also include this reporting rule in the detailed DP06 plan: after every completed task, story, epic criterion, review, test batch or integration, immediately give one compact progress update showing **accepted tasks x/y — %, accepted stories x/y — %, and accepted epic criteria x/y — %**, the item just completed, the change since the preceding update, and the next action/blocker. Use the approved plan's actual denominators; disclose an explicitly approved denominator change. Test/review completion alone does not increase accepted delivery counts. When nothing new is accepted, show unchanged counts and say what evidence or blocker changed. Do not double-count parent and child work or invent a denominator before the plan is defined. The same update may satisfy the 60-second reporting requirement; no duplicate record or reporting framework is needed.

### Continue after approval

The one approved plan covers its scoped implementation, affected tests, fixes, independent reviews, integration, finite genuine qualification, reporting and supported owned resource disposition. Required current internal authority and source bindings still apply. Do not request routine per-task/story/test/retry/handoff approval or silently expand scope.

Before admission, carry the approved plan, this prompt, applicable shared instructions and tracking/active-delivery changes into an isolated branch derived from accepted DP05. Apply only relevant changes and inspect the transfer once. Preserve delivered runtime/configuration/dependencies and unrelated edits; stale scheduling text does not override current user approval or renew expired authority. Do not activate another plan's automation prompt.

On each completion consume the actual result, resolve the named gap, independently review relevant changed work, integrate/close, confirm Linear state and mark accepted plan items `DONE -` with stable IDs and dated evidence. Immediately select the next eligible work. A dispatch, handoff or status answer is not a stopping point. Delegate only useful substantive work or required independent judgment, with actual ACK/START and isolated owned resources. Use bounded waits while healthy work runs; the 60-second updates and 15-minute stall response need no new monitoring framework.

Agreed stop conditions are accepted DP06 completion with settled or supported protected obligations, an explicit user pause, a material scope/template decision needing agreement, or exclusively external prerequisites after eligible independent work is exhausted. A blocker stops its dependent effects; continue other eligible approved work. Preserve original keys, deadlines, attempts, accounting, uncertainty, human fences and failure history. No account/model/API fallback, invented stop evidence, destructive cleanup or revival of revoked grants.

Deliver the cumulative working increment, exact source/package, supported scheduling/concurrency procedure, task/story/epic acceptance counts and percentages, relevant checks, genuine demonstrations, measured performance and resource disposition, then leave the DP07 handoff. Do not implement DP07 or DP09. Full-host/release gates retain their scope. No default-branch merge, normal-session activation, production, source-skill publication or AI Index migration is authorized; AI Index remains read-only scenario evidence without script copies or runtime coupling.
