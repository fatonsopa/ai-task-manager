# Autonomous AI task manager build

This repository is the active development workspace. Its docs directory contains the maintained plans, specifications, acceptance cases and diagrams. Earlier handoff downloads are historical; continue work from these files and the current status record.

Implementation is approved now. Use claude_roni for Claude work and testing; keep the default claude account unavailable until the user says otherwise. Use both frozen current skill versions; audit affected work when the user supplies an update. Every new or updated task prose must pass Technical Writer. The missing second Linear identity blocks live two-user acceptance only.

Read [the Implementation Plan (IP)](BUILD.md), [execution policy](config/execution-policy.json), [architecture](ARCHITECTURE.md), [the complete task and Linear workflow](WORKFLOW.md), [requirements](config/requirements.json), [work items](config/work-items.json) and [acceptance cases](config/acceptance-cases.json). `BUILD.md` includes the expected behavior, delivery sequence, test strategy and operating procedure. The repository address is in the policy file so personal identifiers do not need to appear in Markdown.

The primary Linear credential is already available as `LINEAR_API_KEY` in the application repository's `.env.local`; the builder should reuse it without copying or displaying it. A read-only probe confirmed the expected TST team. No live write has been tested. Core development, mechanical checks, available Codex work and single-user TST checks can proceed without Claude or a second Linear identity. Docker Desktop must be running before dependent database/Linux-container tests.

When the second Linear test account is ready, save its token from an interactive terminal:

```sh
python3 tools/setup_test_credentials.py --secondary-only
```

The helper writes hidden-input values to `$HOME/.config/ai-task-manager/test-credentials.env`, outside Git, with mode 600. The second token must authenticate a different test user with TST access. It is required for live two-user isolation acceptance; missing it cannot be counted as a pass. No product acceptance case has run while preparing this package.

Check package integrity and cross-references:

```sh
python3 tools/check_handoff.py
```

Give the fresh session this instruction:

> Develop and verify the product described in this package. Read START-HERE.md and BUILD.md in full, then load the execution policy, requirement inventory, work graph and acceptance manifest. Begin at P00. Use the supplied public repository, preserve existing content, and stay within USD 100 total incremental spending and 48 elapsed hours. You may implement, test in isolated environments, create run-owned TST fixtures and push code to the supplied repository. Both Claude Code and Codex CLI require actual installed execution evidence. Use assigned workers for substantive tasks, with separate worktrees and databases. Preserve the existing merge script and command intent. Implement and rehearse one complete cutover; never run old and new task-state writers together. Do not activate normal working sessions or deploy production. Freeze acceptance criteria before tuning, retain failures and report unrun cases honestly. Keep durable checkpoints and continue independent authorized work through recoverable failures. Stop new admission before the deadline, preserve unfinished work, and deliver artifacts, evidence and an explicit readiness decision. Do not spend beyond the cap or claim completion if required tests remain failed, blocked or unrun.

The plan authorizes the specified final test batch within its limits. Routine implementation choices and necessary bounded retries do not require another permission request. New scope, extra spending, normal-session activation and production actions remain outside this grant.
