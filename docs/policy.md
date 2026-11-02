# Task coordination policy

Status: Approved principles and responsibilities, consolidated from R01–R38 and the user's implementation decisions. This document does not approve normal-session activation or claim product acceptance.
Authority: The user's scope and decisions govern; the approved product requirements govern conflicting legacy instructions. Exact trusted settings and recorded decisions remain in `config/execution-policy.json`.

## Purpose and scope

Coordinate work through Claude Code and Codex CLI while keeping task ownership, Linear, workers and resources truthful and recoverable. This policy covers managed sessions, project integrations, distribution and the authorized build. It does not authorize production changes, new paid infrastructure, other users' work or activation in normal sessions.

The Spec Driven Development document (SDD), [spec_driven_development.md](spec_driven_development.md), owns the detailed behavior and exceptions. The policy states the principles and responsibilities behind those rules; it does not define another transition engine.

## Definitions

A lead is the main session coordinating the user's work. A worker executes one assigned substantive task. A principal is the authenticated Linear identity. A logical session is the trusted boundary selecting one terminal's authorized work. An attempt is one execution, distinct from its issue and delivery stage. Evidence is an attributable observation bound to the relevant artifact or event; a model's assertion is not evidence by itself.

## Principles

| Principle | Required direction and source |
| --- | --- |
| Coordinate through the lead | The lead may investigate, maintain plans, frame tasks, review prose/evidence and provide narrow coordination support. Assign substantive application work to tracked workers. All worker communication passes through the lead. Coordination does not require an invented task or worker. R01/R36. |
| Authority precedes activity | Validate principal, project, session and current human decisions before reading, changing or starting managed work. Available capacity is not permission to claim unrelated backlog. R02/R04/R07/R11/R12/R27. |
| Give each task explicit ownership | Each independent task owns one assigned worker, worktree and application database today. Separate control metadata from application data. Future parent/role ownership must remain representable without enabling shared databases now. R03/R05/R06/R19/R31. |
| Enforce one policy through code | CLI, MCP, hooks and presentation use the same guarded application operations. Prompts explain the rules; a display label or model-supplied identity cannot grant authority. R15/R27/R33. |
| Report only observed facts | Distinguish startup, progress, result, delivery, remote confirmation, CLI display, lead delivery, AI processing and public reporting. Partial or unknown outcomes remain explicit. R03/R09/R22/R30/R36. |
| Preserve intent and correct defects | Preserve command names, arguments and intended foreground behavior while fully refactoring merge internals into focused domains. Workers own their commits; one merge authority validates and integrates into `dev`. Audit the latest source and CG history, freeze retained and corrected behavior, and verify recovery before replacement. Working legacy code is evidence, not proof that its test selection or sequencing is sound. R08/R28/R32. |
| Recover without losing ownership | Save obligations before external effects. Preserve attempts, human decisions, uncertain writes and checkpoints; inspect reality before retry or cleanup. Unexpected events are refused or staged for resolution, never silently adopted as authority. R10/R12/R14/R30/R31/R37. |
| Activate one authority | Migrate all required state and callers, fence old writers, remove only validated obsolete artifacts, install and validate replacements, then activate one new writer. Rollback must preserve post-cutover facts without dual operation. R28/R34. |
| Bound work and maintenance | Use finite trusted capacity, reviewed batches and per-invocation time and charge bounds. The recorded build authorization lifts overall spending, elapsed-time and retry ceilings while preserving accounting and every attempt. The user imposed no fixed worker count; this never permits unlimited concurrency. Repair/update automation is opt-in, retains regression criteria and safe rollback, and does not upload private evidence without authorization. R06/R18/R20/R26/R35. |
| Require evidence for acceptance | Preserve every required case and host/profile distinction. Failed, blocked or unrun required coverage prevents release; budget or deadline pressure cannot waive it. Task prose receives exact version-bound writing approval. R16/R21–R23/R25/R29/R38. |

## Policy obligations

The tool must enforce scope, lifecycle causes, ownership, evidence and resource limits at the application boundary, including supported direct-tool paths. Apply scope to lists, detail views, events, diagnostics and mutations. Human holds, assignments and acceptance decisions remain authoritative and attributed. An unavailable observer means unknown; it cannot establish that a worker stopped or that an external write failed.

New work follows reviewed issue creation and explicit admission. Delivered issues cannot receive new attempts; further changes use a new linked issue. Detailed transitions, human Review destinations and verified non-code/terminal exceptions belong solely to the Spec Driven Development document (SDD).

Keep private credentials, raw application snapshots and operational evidence outside public artifacts. Restrict test mutations and cleanup to verified run-owned TST resources. Preserve useful legacy safeguards while removing conflicts with the approved product, using the migration inventory rather than blanket deletion.

## Responsibilities and compliance

| Role | Responsibility |
| --- | --- |
| User | Authorize product scope, material policy changes, additional permissions and the final normal-session activation. Existing authorization remains valid within its stated limits. |
| Lead | Interpret requests, investigate within the approved boundary, prepare/review task prose, admit and assign work, route all worker communication, judge evidence and report outcomes. Assignments, scope changes, resource handoffs and acceptance remain lead-coordinated. |
| Worker | Perform its bound assignment in owned resources; send progress, questions, blockers and results through the lead; follow the preserved project merge route. No peer coordination, self-assignment or unilateral scope expansion. |
| Tool and adapters | Enforce trusted capabilities, durable state, actual observations, sole-writer synchronization, safe supervision and scoped presentation. An adapter translates external systems; it does not invent policy or human decisions. |
| Build and release review | Assess the required evidence against frozen criteria and exact artifacts. Keep implementation, component checks and installed/live acceptance distinct; record remaining failures and blocked cells truthfully. |

The Test Plan (TP), [test_plan.md](test_plan.md), owns assessment methods and exit criteria; exact cases remain in `tests/acceptance/cases.json`. The Test Status Report (TSR), [test_status_report.md](test_status_report.md), records current observations. A policy or plan is not a passing result.

## Exceptions

Use only the already-approved guarded exceptions in the Spec Driven Development document (SDD), including human Review destinations, non-code completion and reasoned cancellation/duplication. No emergency, free slot or model preference creates an additional transition or permission.

An expansion of product scope, accounts, credentials or other permissions requires an explicit user decision and versioned requirements/check updates. The recorded lift of overall build ceilings already authorizes continued work in finite reviewed batches; routine evidence-supported continuation needs no new user approval. Merging the final delivery into a pre-existing default branch requires separate authorization. Host permissions still apply. The lead may make routine implementation choices within existing authority; it cannot waive a failed required gate. The approved build permits reviewed public code pushes; public release, normal-session activation and production actions retain their separate authorization boundaries.

## Ownership, review, and related standards

Review this policy when the user changes authority, roles, scope or an approved requirement. Preserve the decision and its affected evidence rather than silently rewriting history. R01–R38 retain their identifiers in `config/requirements.json`; this consolidation does not create a competing requirement registry.

The High Level Document (HLD), [high_level_document.md](high_level_document.md), owns component and trust design. The Implementation Plan (IP), [implementation_plan.md](implementation_plan.md), owns phases, dependencies and rollout. The focused [terminal specification](terminal/spec_driven_development.md) owns sidebar behavior. The future `migrate.md` How-to Guide (HTG) will contain tested script-backed operator commands; it must not invent an executable migration before one exists.
