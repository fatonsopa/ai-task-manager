# Development status

Updated: 1 November 2026.

Implementation is active in the requested repository. The docs directory is the maintained source of truth. The approved limits remain USD 100 total incremental spending and 48 elapsed hours; private run records preserve the actual start, deadline, assignments and reservations.

## Verified components

TST-74 inventory is integrated: 298 mapped contracts, callers and stores, with 16 regression lessons. TST-75 core is integrated as c9ab1e4 after review and an independent rerun of 1,397 focused tests. It supplies guarded task transitions, session ownership, atomic attempt reservations and distinct provider/lead receipts. These tests do not establish host or provider integration.

TST-73 build controls are integrated as 08c0987 after 70 macOS and 70 Linux/aarch64 tests passed. Retained failures led to module, exit-code and process-group fixes. A completed process leader cannot release capacity while its owned descendants remain live or uncertain.

## Active integration

TST-76 is assigned to the Linear adapter worker, and TST-77 covers worker/resource supervision. Each has its own worktree and disposable database. Its scope includes immediate outbound processing, signed durable inbound delivery and policy application. The documented lifecycle connects a user request through Linear, CLI, worker execution, verification, lead acknowledgement and public reporting. The acceptance inventory contains 248 authored scenarios across 14 workflows; authored coverage is separate from executed results.

Codex CLI and the claude_roni subscription login are authenticated. Docker is running. No external model task, live product webhook or end-to-end acceptance case has run yet. No code has been pushed and no normal-session integration has been activated.

## Source baseline

Compatibility work uses c05f40200703e7b8f379a8477502dabd594ba79c on origin/dev-mirror, 72 commits ahead of the local AI Index dev checkout. Preserve the latest merge script unchanged, including its behind-dev precheck warning. Application files remain unchanged.

## Holds and blockers

* The original claude account is unavailable. Use claude_roni without fallback.
* Use both frozen current skills. Audit affected artifacts and rerun impacted checks when the user supplies an update. Task prose is reviewed before Linear publication.
* A second Linear identity is unavailable by user confirmation. Live two-user acceptance remains blocked; other work continues.
* A reachable HTTPS receiver and a concretely scoped TST webhook still need setup and live evidence. A valid local tunnel configuration does not establish delivery.

## Next gates

Integrate the checked build runner, implement the Linear and host/resource adapters, then run the complete request/result path through actual tool-enabled Claude Code and Codex sessions. Keep mechanical, live, behavioral, installation and platform results distinct.
