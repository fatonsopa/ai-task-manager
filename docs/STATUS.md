# Development status

Updated: 1 November 2026.

Implementation has started in the requested repository. The docs directory is the maintained source of truth. The approved operating limits remain USD 100 total incremental spending and 48 elapsed hours; private run records preserve the actual start/deadline, assignments and spend reservations.

## Current work

Repository bootstrap and provider/resource preflight are in progress. The product repository has an initial commit, and Docker is running. Codex CLI authentication and the claude_roni subscription login were checked. No external model task or product acceptance case has run yet.

## Source baseline

The newest fetched development mirror is `c05f40200703e7b8f379a8477502dabd594ba79c` on `origin/dev-mirror`, 72 commits ahead of the local AI Index dev checkout. Compatibility work must inspect that snapshot, including the new behind-dev merge-precheck warning, stack bootstrap checks and changed seed data. Keep the merge script unchanged. Record pinned baseline hashes in the migration inventory before implementing adapters.

## Holds and blockers

- The original claude account is unavailable. Use claude_roni without silent fallback.
- Technical Writer and Skill Builder are being updated; all work invoking, modifying or validating either skill is held until the user releases it.
- A second Linear identity is unavailable by user confirmation. Live two-user acceptance remains blocked; other implementation and tests continue.

## Next gates

Register build assignments in a run-owned TST project, freeze the current legacy contract inventory, then implement the deterministic core, worker/resource lifecycle and provider adapters with reviewed tests. Keep results distinct from planned coverage.
