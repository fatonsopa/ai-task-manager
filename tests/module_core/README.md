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

The integrated run on runtime source `7a7f564bc537338a10cea368a4daf672fadd5e5a` matched all 303 runtime files. It passed the 10 direct CLI/MCP checks. The selected existing API baseline then passed 1,337 cases and failed one; 27 remained unrun. The full invocation took 21.93 seconds. These are local control results, not whole-M1 acceptance.

The required failing case is `tests/storage/test_atomicity.py::test_p08_racing_distinct_tasks_obey_global_and_project_capacity[project]`. Its later same-session task can race ahead of an earlier eligible queue item, so scheduling refuses before the test reaches its expected capacity exception. This does not prove a capacity violation. The original case remains selected and unchanged; its failed result blocks a complete baseline pass.

Omit `--without-baseline` to run the frozen existing API selection in [baseline version 1](baseline-v1.json). That mode currently exits nonzero at the retained failure. It does not silently skip or reinterpret the case. A later bounded correction must reconcile queue eligibility with the capacity-race fixture, preserve the failure, and complete the unrun cases. Do not change runtime policy to satisfy the old test assumption.

## Coverage and remaining work

The 12-check demonstration has **10/12 verified**, separate from module acceptance:

| M1 submodule | Verified demonstration checks | Limits |
| --- | --- | --- |
| Lifecycle/scope | 6/7 | Create/read, explicit queue, lifecycle refusal, missing-human refusal, scope and stale-state controls pass. The positive human-hold API baseline remains unrun. |
| Dependencies/capacity | 1/2 | Missing evidence remains blocked. The combined dependency/capacity baseline is incomplete after the retained race failure. |
| Persistence/recovery | 2/2 | Cold clients and lost committed replies preserve task/effect identity. Attempt, pending-effect and controller recovery are broader obligations; the existing restart baseline remains unrun. |
| CLI/API | 1/1 | Installed CLI and MCP return the same scoped data and schemas. This is not proof that every public operation has its required producer. |

The full module remains unaccepted. The following obligations are not proved by this delivery:

- Successful installed human instruction capture, hold/release and human review decisions. The default `SessionObservations.verify_instruction` refuses; a negative `human_ref` test cannot prove its missing producer.
- Dependency cycles, predecessor milestones, project capacity across hosts and positive human-hold behavior in the unrun baseline. Queueing leaves execution idle and dependency readiness unknown; it does not authorize work.
- A canonical imported issue claim across sessions, delivered-task/follow-up fences, all approval tampering/version cases, and all interruption/ownership recovery cases on this candidate. The baseline maps existing coverage but does not claim every M1 requirement is closed.
- Public dependency mutation, capacity configuration and direct reservation commands. These are Core/code-only interfaces; this CLI schema exposes no corresponding operation. `worker_start` needs its configured worker integration, which this fixture deliberately lacks.
- Real provider/host connections, fresh semantic writing review, independent source review, integration of this tooling, and the cumulative accepted-module demonstration. Accepted recovery has separate lead-owned evidence and is not reimplemented or regraded here.

These gaps affect completion of R03/R04/R06/R07/R09/R11/R13/R14/R15/R20/R27/R30/R38 as a combined module; individual passing checks retain their narrower meaning. The Implementation Plan (IP), [implementation_plan.md](../../docs/implementation_plan.md), owns the six common acceptance conditions and module/connection boundaries. This baseline does not replace the original acceptance inventory or the Test Plan (TP), [test_plan.md](../../docs/test_plan.md).

If a run fails, retain its directory. Inspect its recorded failure before selecting another bounded invocation. The tool never resets an existing store, retries a native worker, changes policy, or promotes a refusal into a successful producer result.
