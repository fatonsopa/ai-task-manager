# AI task manager development

Build the approved standalone task coordinator for Claude Code and Codex CLI. Keep the reusable core independent of application-specific scripts and host integrations. The approved private build handoff is supplied by the lead when needed; it is not a runtime dependency of the distributed product.

Use one tracked assignment per worker, a separate Git worktree and a separate disposable fixture database. The lead coordinates and reviews; assigned workers perform substantive implementation and investigation. Delegate only assignments registered by the lead. Workers must not spawn additional workers or write to another worker's scope.

Technical Writer and Skill Builder are being updated. Do not invoke, read, modify or validate either skill until the user explicitly releases that hold. Continue independent engineering and tests.

Use the Claude account profile named claude_roni. Automated invocations use the unmodified claude executable with CLAUDE_CONFIG_DIR set to the resolved .claude_roni directory under the current user's home. The default claude account is unavailable; do not use it or silently fall back to it. Authentication checks do not establish successful model execution.

Preserve the AI Index merge script unchanged. The lead provides a pinned current source commit for compatibility work. Do not change the application checkout or run production deployments. Test project integrations in disposable copies only.

Enforce declared task ownership, status transitions, evidence and isolation in code. In Review supports every configured destination with authorization and destination-specific evidence. Further work on delivered issues uses a new linked issue. Activation uses one complete cutover; never run two live task-state writer systems together.

Run focused tests during development and the complete required suite for the final candidate. Retain failed attempts and distinguish blocked/not-run cases from passes. Never fabricate live provider or model evidence. Do not commit credentials, private source snapshots, machine paths or personal names in Markdown. Public pushes are authorized for reviewed code; normal-session activation and production changes remain outside scope.
