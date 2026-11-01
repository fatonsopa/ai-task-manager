# AI task manager development

Build the approved standalone task coordinator for Claude Code and Codex CLI. Keep the reusable core independent of application-specific scripts and host integrations. The maintained plans, specifications, acceptance cases and diagrams under docs/ are the source of truth. Read docs/START-HERE.md and the task-relevant sections before work. Update docs/ when implementation decisions or evidence change; earlier workspace artifacts are historical. Operational records and credentials stay under ignored local storage.

Use one tracked assignment per worker, a separate Git worktree and a separate disposable fixture database. The lead coordinates and reviews; assigned workers perform substantive implementation and investigation. Delegate only assignments registered by the lead. Workers must not spawn additional workers or write to another worker's scope.

Use the current frozen Technical Writer and Skill Builder versions. Every task title, description, acceptance criterion and written progress update must pass Technical Writer with the appropriate reference and Writing Style guide before publication. Scripted status/ownership fields remain governed by the execution policy. When the user announces a new skill version, audit affected work, revise only what needs changing and rerun impacted checks; preserve earlier version-bound evidence. Do not modify either source skill as part of this product build.

Use the Claude account profile named claude_roni. Automated invocations use the unmodified claude executable with CLAUDE_CONFIG_DIR set to the resolved .claude_roni directory under the current user's home. The default claude account is unavailable; do not use it or silently fall back to it. Authentication checks do not establish successful model execution.

Preserve the AI Index merge script unchanged. The lead provides a pinned current source commit for compatibility work. Do not change the application checkout or run production deployments. Test project integrations in disposable copies only.

Enforce declared task ownership, status transitions, evidence and isolation in code. In Review supports every configured destination with authorization and destination-specific evidence. Further work on delivered issues uses a new linked issue. Activation uses one complete cutover; never run two live task-state writer systems together.

Run focused tests during development and the complete required suite for the final candidate. Retain failed attempts and distinguish blocked/not-run cases from passes. Never fabricate live provider or model evidence. Do not commit credentials, private source snapshots, machine paths or personal names in Markdown. Public pushes are authorized for reviewed code; normal-session activation and production changes remain outside scope.
