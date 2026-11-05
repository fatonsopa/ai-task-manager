# M1 installed task-core demonstration

This developer entry point runs the installed task API through cold CLI clients and an actual stdio MCP client. It creates a scoped task, explicitly queues it, checks refusals without changing Core state or provider writes, loses one committed create reply, and recovers the same result in fresh processes.

The provider is the existing loopback HTTP contract fixture. Writing uses the configured Technical Writer package and an explicitly authored review control. These observations establish neither live Linear behavior nor fresh model review. The transport endpoint override and lost-reply injection are declared test seams; they are not an offline product mode. Setup uses `installation.sessions.setup`, sealed configuration and the existing WritingService operations. The demonstration makes no manual database edits and constructs no worker, stop or delivery receipts.

## Run the demonstration

Use Python 3.13, `uv`, the repository checkout, and an approved Technical Writer package. Set `TECHNICAL_WRITER_PACKAGE` to that package's directory. Run from the repository root; choose new output and environment paths.

```sh
uv venv .local/m1-demo-venv --python 3.13
uv pip install --python .local/m1-demo-venv/bin/python . pytest
.local/m1-demo-venv/bin/python -I tools/module_core_demo.py \
  --writer-root "$TECHNICAL_WRITER_PACKAGE" \
  --output .local/m1-demo-run \
  --without-baseline
```

The tool rejects a source-checkout import or a package whose runtime hashes differ from the checkout. It creates an owned disposable database by default. `--database` may instead select an already-owned, empty, private disposable file; its original physical identity must survive the run. Existing output directories and nonempty databases refuse. No original application or production state is used.

Success writes `local_demonstration_passed_connections_pending` to `result.json`. Inspect `project/commands.jsonl` for each exact command, input, response, exit code and before/after effect hashes. `project/mcp.json` records schema/data parity and observed MCP cleanup. `project/cleanup.json` records the stopped loopback fixture. `source.json` retains the commit, interpreter, runtime/package file hashes, test sources, writer pins and command. Output is private evidence; do not publish its capabilities or configuration.

Each CLI operation opens the existing installed application in a fresh process. The bootstrap changes only the loopback transport endpoint; the missing installed observation and worker producers continue to refuse. The optional lost-reply seam exits after the application has returned its committed result and before stdout publishes it. The retry must preserve task identity and the provider mutation count.

## Frozen checks and retained failure

[Expectations version 3](expectations-v3.json) freezes 12 demonstration checks and the public error categories. Versions 1 and 2 preserve earlier assertions. Two failed authoring attempts confused queue membership with execution state, and classified an illegal transition as invalid input. Version 3 records the source-based corrections; it does not change product policy.

The integrated demonstration on source `283bf5f942e2c6f795f674316dc847027aafbe6a` matched all 303 runtime files, passed the 10 direct CLI/MCP checks and passed all 1,365 selected API baseline cases without failures or skips. The complete invocation took 18.3 seconds. Together these verify the 12 declared demonstration checks in isolation; they do not establish whole-M1 or live-provider acceptance.

The earlier run on `7a7f564bc537338a10cea368a4daf672fadd5e5a` passed the direct checks and 1,337 baseline cases, failed one and left 27 unrun in 21.93 seconds. Its failing case, `tests/storage/test_atomicity.py::test_p08_racing_distinct_tasks_obey_global_and_project_capacity[project]`, allowed two same-session tasks to race before the later task was eligible. The corrected fixture uses independently eligible sessions and separately verifies FIFO ordering; runtime policy was unchanged. Retain the original failure and the later verification as separate evidence.

Omit `--without-baseline` to run the frozen existing API selection in [baseline version 1](baseline-v1.json). Both modes passed on the integrated candidate above. The runtime-file comparison excludes exact `.DS_Store` metadata filenames; changed or missing runtime files still refuse. Final database replacement or unreadable identity produces retained failure evidence and a nonzero exit. Earlier failed runs remain retained.

## Coverage and remaining work

The 12-check demonstration has **12/12 verified**, separate from module acceptance:

| M1 submodule | Verified demonstration checks | Limits |
| --- | --- | --- |
| Lifecycle/scope | 7/7 | Create/read, queue, refusal, scope and stale-state controls pass. The positive human-hold check uses the declared Core API fixture, not installed human-input capture. |
| Dependencies/capacity | 2/2 | The dependency/capacity baseline passes. The direct demonstration leaves execution idle and blocks unknown dependency evidence; queue membership does not authorize work. |
| Persistence/recovery | 2/2 | Cold clients and lost committed replies preserve task/effect identity. The selected restart baseline passes; broader attempt/controller recovery has separate evidence. |
| CLI/API | 1/1 | Installed CLI and MCP return the same scoped data and schemas. This does not prove that every operation has its required production authority source. |

The [remaining Core guard mapping](remaining_guards_v1.json), integrated at `63add9c99dae350c9978e19f2c5ab77fd7518e28`, records 53 independently reviewed controls for canonical ownership, delivered-task/follow-up fences, approval tampering/version checks and interruption/ownership recovery. Its initial private-runner failure and corrected single-case retry remain retained. These results verify Core consumption rules; they do not supply a missing production issuer.

Core behavior is verified in isolation; real connections remain pending. The integrated follow-up path uses an explicitly enrolled `delivered_followup` capability. It creates a linked Backlog child without reopening its parent, queueing the child or starting an attempt. Cached and supplied proofs revalidate the original enrollment and configuration before an uncommitted effect. Exact committed retries recover the original result. Existing enrollments gain no new capability automatically.

The correction passed 67 focused author checks and four independent review controls. The original cached-proof source-continuity failure remains retained. The integrated demonstration and 1,365-case Core baseline passed after that correction. These overlapping selections must not be added as a coverage total.

The remaining connection obligations are:

- Actual human-instruction capture, hold/release and human review decisions through the supported hosts. The default `SessionObservations.verify_instruction` refuses; positive Core fixtures establish consumption rules only. The Implementation Plan (IP) permits those declared external fixtures for M1 isolation and requires real producers in the assigned host/connection checks.
- Actual provider and host connections, fresh semantic writing review, worker execution and lead reporting. Follow-up controls use the production issuer/router with authored local provider observations; they do not prove live Linear or verified worker delivery.
- Dependency mutation, capacity configuration and direct reservation remain Core APIs rather than CLI commands. The demonstrated CLI exposes scoped projections and queue operations. Actual `worker_start` behavior belongs to the configured worker connection.

The Implementation Plan (IP), [implementation_plan.md](../../docs/implementation_plan.md), owns the six common completion conditions and module/connection boundaries. Retained evidence covers the declared M1 selection for R03/R04/R06/R07/R09/R11/R13/R14/R15/R20/R27/R30/R38. This demonstration does not replace the original acceptance inventory, full release obligations or the Test Plan (TP), [test_plan.md](../../docs/test_plan.md).

If a run fails, retain its directory. Inspect its recorded failure before selecting another bounded invocation. The tool never resets an existing store, retries a native worker, changes policy, or promotes a refusal into a successful producer result.
