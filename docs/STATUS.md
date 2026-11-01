# Development status

Updated: 1 November 2026.

Implementation has started in the requested repository. The docs directory is the maintained source of truth. The approved operating limits remain USD 100 total incremental spending and 48 elapsed hours; private run records preserve the actual start/deadline, assignments and spend reservations.

## Current work

Repository bootstrap and provider/resource preflight are in progress, and Docker is running. TST-74 inventory is integrated: 298 mapped contracts/callers/stores and 16 regression lessons. TST-73 build controls passed 52 initial tests and are being refactored into focused modules before acceptance. Codex CLI authentication and the claude_roni subscription login were checked. No external model task or product acceptance case has run yet.

## Source baseline

The newest fetched development mirror is `c05f40200703e7b8f379a8477502dabd594ba79c` on `origin/dev-mirror`, 72 commits ahead of the local AI Index dev checkout. Compatibility work must inspect that snapshot, including the new behind-dev merge-precheck warning, stack bootstrap checks and changed seed data. Keep the merge script unchanged. Record pinned baseline hashes in the migration inventory before implementing adapters.

## Holds and blockers

- The original claude account is unavailable. Use claude_roni without silent fallback.
- Use both frozen current skills. When the user provides an update, audit affected work and rerun impacted checks. Task prose is reviewed before Linear publication.
- A second Linear identity is unavailable by user confirmation. Live two-user acceptance remains blocked; other implementation and tests continue.

## Next gates

Register build assignments in a run-owned TST project, freeze the current legacy contract inventory, then implement the deterministic core, worker/resource lifecycle and provider adapters with reviewed tests. Keep results distinct from planned coverage.
