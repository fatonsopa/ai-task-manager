# Task coordination policy audit

Status: Located source review and proposal for agreement. Application instructions, settings and scripts have not been rewritten. The approved product plan and executable tool take precedence over conflicting legacy rules; protected commands and merge intent remain unchanged.

## Evidence and limits

The application checkout is clean at `15992e2be8b2fe47d4646f92e62c90888a332619`. A read-only fetch found development mirror `24d05ac23420b0bb2b062430ff4e9b9c78727b81`, 81 commits ahead. The instruction files and inspected Linear/agent scripts have identical bytes across these two versions. Paths and line numbers below refer to that application source unless identified as product documents.

| Source | Lines | SHA-256 |
| --- | ---: | --- |
| `.claude/standards/task-management.md` | 2,082 | `7579f3b782ac3c2acfcb857dc48d395058dc2fd390c87f38d953688923523600` |
| `.claude/output-styles/lead-engineer.md` | 87 | `bfe5cadf14e8a047b97d0656bfad31fe3b63af5debd556444fda7dd1e7e9b184` |
| `CLAUDE.md` | 3,180 | `8f18e3a6a703e6d5e4eb5f6adec7e1b0024ceae241970554434d8e1bf2ea4517` |
| `.claude/settings.json` | 418 | `a303408f808f99ea74904ce34853fc0660e63f9023527ac5a88b33d60ffcaae2` |

Product requirements were captured from root commit `5ad83bde32ec5e28e6bc7357acd33f0660ed374d`, including `docs/BUILD.md`, `docs/WORKFLOW.md`, `docs/TERMINAL-UI.md` and execution policy. The frozen checklist, source hashes, inspected script spans, configuration projections and fetched-commit evidence are retained privately under assignment `ROLE-AUDIT/TST-80`.

This is a focused static audit of the cited contracts and entry points. It does not reproduce the reported model behavior or establish its root cause. No model pilot, live task mutation, database operation, pipeline or installed-host test ran. File size alone does not establish a model-context or quality failure.

## Located findings

### F01. The role file is available, but its activation is not established

`lead-engineer.md:4` already sets `keep-coding-instructions: true`; retain it. `task-management.md:1558–1564` requires manual style selection. Project shared/local settings and the inspected approved account profile have no selected `outputStyle`. No launch override, loaded plugin policy or running-session style was verified, so this is a missing reproducible activation guarantee, not proof that the user's session lacks the style.

Bind the coordinator role to managed session startup and verify the actual loaded configuration. Keep display-style selection separate from task authority. Anthropic documents style selection and its instruction limits in [Output styles](https://code.claude.com/docs/en/output-styles) and configuration overrides in [Settings](https://code.claude.com/docs/en/settings).

### F02. Task admission happens before actual agent startup

`scripts/linear/subagent_guard.py:397–424` plans an `in_progress` owner change; `427–452` writes the native task and synthesizes a `TaskUpdate`; `472–499` performs that work from the `PreToolUse` path at `2562`. The actual Agent call follows the hook. A failed launch can therefore leave a task started before a matching worker exists.

Replace this sequence at cutover with durable reservation and Starting, followed by actual worker/resource evidence and matching Linear acknowledgement before the work lease. Preserve explicit issue naming, atomic owner comparison and retained write failures.

### F03. The admission guard explicitly continues after its own failure

`subagent_guard.py:2701–2704` reports an unexpected exception and exits 1. Its project registration, `.claude/settings.json:109–114`, supplies no failure-blocking override. Under the documented default, this is a nonblocking error path, even though ordinary policy refusals return a denial.

Make managed admission fail closed in the controller and test crashing, missing and timed-out hook paths. Anthropic documents `exit 2`, failure behavior and version-dependent `onFailure` in [Hooks](https://code.claude.com/docs/en/hooks). This source finding does not establish that the user's session encountered that failure.

### F04. A model-selected worktree flag determines which starts claim a task

`subagent_guard.py:390–405` exempts non-worktree and nested helpers from `starts_work`; the capacity check at `2523–2527` also keys on `isolation: worktree`. Some exemptions intentionally support observation or work within a parent's assignment. They do not establish coverage of every managed launch surface.

Authorize worker launches by registered task/attempt capabilities and verified host mode. Preserve legitimate observation helpers explicitly. Test native Agent, resumed/forked helpers, shell/SDK paths and both supported hosts rather than infer admission from a prompt flag. Anthropic describes distinct contexts and native limit exceptions in [Subagents](https://code.claude.com/docs/en/sub-agents).

### F05. New issues can skip the approved initial Backlog record

`task-management.md:230–240` and `scripts/linear/new_work_place.py:63–81` choose Todo directly for incidents, urgent work, plan steps, decisions and blockers. The approved product requires initial Backlog followed by explicit queue admission.

Treat these shortcuts as migration defects. Preserve urgency, relationships and priority, then perform the two explicit lifecycle steps through the new API. This rule is already approved and is not a new choice for agreement.

### F06. Free capacity can pull work outside the session's admitted queue

`task-management.md:1534–1547` directs the capacity check to find open Urgent Linear issues even without a local task and refuse stopping until an agent starts. Its reader is `subagent_guard.linear_incidents` at `2216`; `session_check` supplies it at `2406`.

Free slots must select only authorized admitted work. Surface a newly discovered incident as an intake proposal with its priority and ownership; spare capacity alone must not claim it. Retain existing protections for other users, blockers and human holds.

### F07. The legacy transition model differs from the approved one

The substantive differences are in `scripts/linear/status_policy.py`: A10 at `598` pauses In Progress to Todo; A8/A8a at `585–588` restrict hold exits; A18b at `636–637` reopens In Dev to In Progress; A19/A20 at `638–640` retain the old restricted fallback. The reopen command remains documented at `task-management.md:1461`.

Generate the new transition reference from the sole policy: confirmed Backlog/Todo admission, guarded hold/resume, every configured In Review destination, verified delivery, explicit non-code/terminal exceptions and a new linked issue for further delivered work. Preserve `issue_cli.py reopen <id> --why "<words>" [--next "AI: …"]` as a compatibility surface routed to the approved follow-up intent; never reopen delivered work silently.

### F08. Legacy utilization rules need the new resource limits

`task-management.md:1487–1532` demands maximum occupancy and repeated Stop refusal. `agent_capacity.py:731–784,837–892` uses CPU readings, allows an unconditional minimum and falls back to a CPU-derived ceiling when those readings are unavailable. `task-management.md:1571–1574` excludes merging agents from limits.

Use the tool's finite, trusted capacity and budget reservations, including unresolved ownership. A blocked prerequisite, approval, unavailable host, unknown process or exhausted budget is a legitimate visible wait. Preserve merging as In Progress with a verified activity, as `docs/TERMINAL-UI.md` requires; a display or accounting exception must not erase resource ownership.

### F09. A worktree still defaults to the shared application database

`scripts/worktree-setup.sh:4–6` and `task-management.md:1922–1927` retain the shared stack until `/setup-worktree-db` is requested. That conflicts with the current independent-worker resource policy.

Managed resource preparation must explicitly provision and verify task-owned database/worktree resources before granting work. Retain the existing `worktree-db.sh` lifecycle and the merge script's stop/removal behavior; do not add competing cleanup or hold its database alive during merge.

### F10. Legacy synchronization and reporting are weaker than the new contract

Outbound task changes already use a journal and immediate drain (`subagent_guard.py:446–452`). Inbound board checks still use prompt/turn hooks and the ten-minute systemd check in `task-management.md:1370–1415`; `linear_sync.py:7790` sets `SYNC_EVERY = 600`. `lead-engineer.md:48–58` and `task-management.md:1684–1685` require prompt result reporting but do not distinguish the new receipt stages.

Preserve immediate durable outbound work and replace normal inbound polling with authenticated events applying the same policy; reconciliation repairs gaps. Keep queued notification, host delivery, AI acknowledgement and public report separate. Native completion or injected context cannot substitute for observed AI processing.

### F11. Account/session discovery is still an inference path

`scripts/linear/claude_home.py:38–69` discovers configuration folders from process environments, then falls back to the default folder when discovery is unavailable or ambiguous. The code documents the macOS and multiple-profile limits. `linear_sync.py:291` uses that result for native task storage.

Bind the provider principal, project, logical session and approved host profile explicitly. Preserve the useful invariant that an unreadable detector means unknown, never nobody. Test two accounts and resumed sessions; neither a working directory nor a native task folder grants authority over another session's work.

### F12. Lead judgment is mixed with absolute communication and verification rules

`lead-engineer.md:62–74` delegates all work once a task exists; `task-management.md:1567–1570` still says the main session holds one in-progress task. Pipeline exceptions exist at `lead-engineer.md:50–51`. Its `52–58` forbids peer messaging, and `75` bans rerunning worker checks outright.

State the already-authorized boundary directly: the lead investigates, briefs, prioritizes, records decisions, reviews prose/evidence and performs risk-based integration verification; independent application work is delegated. Keep protected pipeline/recovery routing as a lead command capability, not another task-state writer. Reuse matching verified results, while permitting justified targeted rechecks. The peer-message recommendation below needs agreement.

### F13. Repeated policy and incident history obscure the active contract

The task standard is 260,523 bytes; root instructions are 331,825 bytes. `task-management.md:1837–1841` even requires duplicate wording parity, while `CLAUDE.md:2873–2890` says incident stories belong on their issues. These are identifiable maintenance costs, not a measured explanation of hallucination.

Keep one short current contract, generated command/transition references, and linked regression rationale. Map every removed rule and dependent heading before replacement. Standing best-practice permission in `task-management.md:1848–1866` cannot override the user's newer approved tool or protected command choices.

## Preserve these safeguards

Retain task/issue IDs instead of title matching; one assigned substantive task per worker; human hold/assignment precedence; independent completion evidence for partial work; unknown-state protection; owned scratch/resource isolation; artifact-bound regression proof; and command/help parity. Preserve the merge script's bytes, arguments, foreground call, result meanings and cleanup ownership. Do not introduce a replacement merge queue or approval step.

The fetched nine-commit delta after `c05f40200703e7b8f379a8477502dabd594ba79c` changes database-bound E2E setup and stack discrimination (CG-3486/CG-3493), edition selection (CG-3492), and application job filtering/proof (six CG-3476 commits). Resource/test adapters must use those current database selectors. It changes none of the audited Linear/agent rules.

`scripts/merge-to-dev.sh` is byte-identical at the prior pin and the fetched pin: SHA-256 `b47d53d4f9861e2a22e1412dd1d9b8c48407bf177c232cb97ff4770acd307022`; their merge-script diff is empty. The older checkout's merge file differs, so copying from that checkout would select the wrong compatibility artifact.

## Proposed responsibility boundaries

| Component | Responsibility |
| --- | --- |
| Executable tool and project adapters | Enforce identity, intake/queue/start, transition causes, capacity, resources, writing receipts, evidence, sync, audit and recovery. Observe protected project commands through their existing routes. |
| `task-management.md` | Explain the current lifecycle, roles, guarded exceptions, protected command map, truthful state meanings and recovery references. Link generated detail. |
| Portable lead role | Perform bounded triage, brief and supervise workers, prioritize admitted work, inspect evidence, route decisions and report verified outcomes. Keep assignment/scope/resource handoff and acceptance with the lead. |
| Worker role | Execute one bound assignment, keep its own resources, report evidence/blockers, and run its preserved merge route. |
| Claude output style | Set voice, response structure and concise status language; retain valid metadata. Role reminders may point to the shared contract. Selection cannot grant task authority. |
| Generated help and reference | Derive command signatures, transition tables, status/activity labels and error explanations from implemented schemas and policy. |

Anthropic permits session roles in an output style; this proposal does not claim otherwise. The coding-instruction field applies to the full prompt; no running prompt mode was verified here. Non-fork subagents have separate prompts, so worker rules must reach their actual role packets. Hooks act only at their supported boundaries; `SubagentStart` cannot block creation. These limits require host-specific evidence.

## Compact proposed outlines

For `.claude/standards/task-management.md`: authority and scope; roles; intake/queue/execution/delivery; human decisions and dependencies; protected commands; evidence and reporting; recovery/cutover links. Keep status matrices, detailed flags and regression history in maintained references.

For the lead role: purpose and permitted investigation; task framing and reviewed intake; admission and briefs; supervision and safe waits; evidence judgment and integration checks; decisions and result reporting; protected pipeline/recovery routing. No arbitrary line-count exception defines coordination work.

For `lead-engineer.md`: valid frontmatter; lead with the outcome; separate verified/pending/blocked/uncertain facts; identify task and next required action concisely; link the role/contract. Do not duplicate a lifecycle table.

For generated help: implemented commands and arguments; legal causes and evidence guards; status versus activity; scoped error/recovery explanations. For `docs/migrate.md`: prerequisites and validated backup; exact manifest/authority fence; obsolete removal; replacement installation/validation; activation; interrupted-step recovery and rollback.

## Candidate removal and replacement map

This is a proposal to refine against the existing migration inventory, not a deletion manifest or execution instruction.

| Candidate | Proposed disposition at cutover |
| --- | --- |
| Obsolete implementations in `scripts/linear/linear_sync.py`, `subagent_guard.py`, `agent_capacity.py`, `status_policy.py`, `new_work_place.py`, `finished_task_close.py`, `rules_copy.py` | Fence and remove manifest-listed obsolete implementation files. Where external callers require a path, install a tested thin compatibility replacement after removal; never keep the old engine active beside it. |
| Old task/agent state-writing hook registrations and native task mutation routes | Remove owned legacy registrations; install only validated managed guards/routes. Keep unrelated application safety hooks. |
| `scripts/sync-watcher/linear-sync-watch.service` and `.timer`, their installed instances and running copies | Disable/fence the specific legacy writer instances; retire obsolete templates only after caller inventory. Reconciliation remains available under the new authority. |
| Task-management standard, lead style and relevant `CLAUDE.md` workflow sections | Replace through the agreed source mapping. Preserve paths and unrelated application rules; do not delete the `.claude` directory. |
| `issue_cli.py`, `/sync-tasks` and deployment skill entry points | Retain required command surfaces through mapped replacements; remove their old policy implementations. Preserve `#push`, `#release`, `#hotfix` and `/sync-with-dev` intent and flow. |
| `scripts/merge-to-dev.sh`, `worktree-setup.sh`, `worktree-db.sh`, current stack selectors and application safeguards | Retain the verified project procedures. Configure managed defaults through adapters and preserve merge bytes. |
| Historical lessons and regression evidence | Retain traceable source references; remove repetitive loaded prose only after its preservation mapping is reviewed. |

The migration script must validate backup and prepared replacements, fence old writers, remove only manifest-listed obsolete artifacts, install replacements, then validate and activate one new authority. The new writer stays disabled throughout removal/replacement. An interruption must resume safely or restore a validated backup without enabling both writer sets. `docs/migrate.md` must contain tested implemented commands; this audit invents none.

## Agreement and verification gates

The lead boundary and protected command routes above reflect existing authorization. Backlog-first intake, delivered follow-ups, review exits, per-session ownership and the approved sidebar are settled requirements. The actual new recommendation is to allow peer technical clarification when journaled/copied to the lead, while keeping assignments, scope changes, resource handoffs and acceptance lead-only. Application document replacements and that recommendation await agreement.

Before cutover, require:

* Admission tests with missing task, wrong scope, failed/unknown launch, hook failure and every supported native/shell/API path; no pre-start In Progress or competing worker.
* Full shared-policy transitions, partial-work proof, human precedence, explicit queueing and capacity/resource failure tests; no automatic backlog filling.
* Two-way provider events, exact task/progress writing receipts, ignored foreign delivery, uncertain-write recovery and separate lead/user receipts. Keep missing two-user or actual-host evidence blocked.
* Real Claude and Codex sessions showing the loaded role/profile, owned task list and actual startup. Verify style-independent enforcement, supported resume/fork boundaries, legitimate waits and risk-based review.
* Byte/argument/flow checks for protected commands, current database selector integration, complete caller parity and C32/C33/B14 cutover interruption/removal tests. Preserve merging and blocked activity semantics from the approved terminal specification.

This report was checked with the frozen Technical Writer and `current_host_self_review`. That identifies a source/structure/style review of this audit, not independent model review or execution of the proposed regression and installed-host gates.
