# Initial workflow preparation

## Before you begin

Use this How-to Guide (HTG) to install a candidate and prepare approved work before the first task-bound owner starts. The trusted operator performs setup. Ordinary CLI and MCP calls retain their owner requirements. This trusted command is not a model tool or an automatic fallback. This procedure creates no product worker and does not activate normal sessions.

Use the reviewed DP01 wheel, its locked runtime requirements and matching source/package manifest. Install them into a new CPython 3.12-or-later environment, then run its absolute `aitm` executable from an empty project with `PYTHONPATH` unset. The installer must provide the approved Technical Writer snapshot, its complete file inventory and an approved Python interpreter with that snapshot's validator dependencies. Preserve a virtual environment interpreter's invocation path; resolving its final symlink can lose its dependencies. Hashes identify the approved files; collecting hashes alone does not approve new guidance or executable code.

You need an authorized Linear human credential, explicit workspace/TST team/project UUIDs, and a selected agent connection for that human and logical session. Use a disposable authorized project for qualification. The current setup port supports the TST preview boundary; this guide does not establish general-team or host-distribution acceptance. Keep credential values out of requests, logs and source control.

Choose a short physical private state path outside the project, installed package and caches. Its parent must already exist; the state directory must be absent or empty and owned by you with mode 700. The generated `owner.sock` path must fit within 100 encoded bytes. Keep the configuration at a distinct direct-child filename such as `service.json`. Authoritative files use private permissions. Do not reuse a copied store, old session grant or expired deadline.

## Steps

### Install the candidate

Replace the capitalized placeholders with the reviewed artifact paths. The requirements file must be the locked runtime export bound to this wheel, not an editable checkout dependency list.

```sh
uv venv --python APPROVED_PYTHON ENVIRONMENT
uv pip install --offline --require-hashes --python ENVIRONMENT/bin/python -r RUNTIME_REQUIREMENTS
uv pip install --offline --no-deps --python ENVIRONMENT/bin/python WHEEL
```

Use the installer's approved uv version and populated offline dependency cache. Retain installation output and the dependency inventory. From the empty project, run the installed executable with `--help`, then verify that `ai_task_manager.__file__` resolves inside the new environment. Stop if it resolves to a source checkout or another installation.

### Set up the session

1. Save a private JSON setup manifest, separate from the state directory. The top-level object has exactly these fields:

   | Field | Required value |
   | --- | --- |
   | `version` | Integer `1`. |
   | `state_directory` | Absolute physical private state path described above. |
   | `linear.expected` | Object containing canonical `viewer_id`, `workspace_id`, `team_id`, `project_id`, and `team_key: "TST"`. Setup verifies actual provider identity and membership. |
   | `linear.agent_connection` | Object containing `connection_id`, `workspace_id`, `principal_id`, `logical_session`, `agent_id`. Workspace, principal and logical session must match the expected human/session. The selected agent must be an active provider app. |
   | `linear.credential` | Exactly one supported reference described below. |
   | `logical_session` | Explicit nonempty session name, matching the connection. |
   | `writer` | Object containing absolute physical snapshot `root`, absolute Python invocation path `python`, executable-byte `python_sha256`, and the complete approved relative-file-to-SHA-256 `pins` inventory. |
   | `capacity_policies` | Nonempty list of objects with `host` (`codex` or `claude`), positive integer `host_limit` and `project_limit`. Hosts are distinct; the project limit agrees across hosts. |
   | `actions` | Exactly `["enqueue"]`. |
   | `deadline` | Original finite future Unix time in seconds. It bounds delegation, including verification time. |
   | `operation_key` | Explicit nonempty key identifying this setup intent. |

   Choose either `{"environment":"AITM_CREDENTIAL_PRIMARY"}` with that variable already available to each command, or `{"file":"ABSOLUTE_PRIVATE_FILE","key":"LINEAR_API_KEY"}`. File references also support `LINEAR_TEST_USER_A_TOKEN` and `LINEAR_TEST_USER_B_TOKEN`. Environment names must use the `AITM_CREDENTIAL_` namespace. Use the existing approved credential source; do not paste its value into the manifest. Unknown fields, factories, endpoint overrides, raw capabilities and supplied approval verdicts are refused.

2. With the credential reference available, replace `AITM`, `CONFIG` and `MANIFEST` with absolute paths and run:

   ```sh
   AITM --config CONFIG session setup --input MANIFEST
   AITM --config CONFIG --json session doctor
   ```

   Setup must return `ok: true` with a confirmed enrollment and saved capability. Doctor must return `ready: true` and passing checks for installation, writing, private paths, session authority, claim store, capacity, preparation, unchanged configuration and current authority. Doctor reads provider/local state; it does not repair files or grant authority. A nonready result exits with code 4. Stop before publication if either command fails.

3. Retain the original manifest and results. Exact setup replay revalidates the same current installation without renewing it. Changed input, package source, scope, capacity or expired authority cannot replace it. A partial setup keeps its intent and files for trusted reconciliation; do not delete them and repeat the effect.

### Prepare an approved task

1. Save one private JSON request with exactly `operation` and `data`. The data object follows the public operation contract. Allowed operations are `writing_prepare`, `writing_review`, `writing_finalize`, `task_submit`, `task_show`, `task_list`, and `task_action` with `action: "enqueue"`. Delivered follow-up submissions are refused.
2. For an initial read, use the complete request `{"operation":"task_list","data":{}}`, then run:

   ```sh
   AITM --config CONFIG workflow prepare --input REQUEST
   ```

   A fresh session returns an empty list. All preparation calls use this same envelope and installed command. Keep each exact request and returned identifiers.
3. Complete writing prepare/review/finalize using the current pinned issue guidance and exact provider title/body. Review meaning, language and preservation; a passing structural validator does not supply semantic approval. Submit only that exact currently approved task. Wait for confirmed Backlog and matching canonical provider identity, human assignee and selected agent delegate before treating linkage as complete.
4. Explicitly enqueue the confirmed task with its current expected version and decision revision. Confirm Todo with `task_show` and scoped `task_list`, and compare independent provider readback. Backlog submission alone does not queue work. Initial preparation does not authorize execution or start an owner.

Preparation requires absent owner record/socket, no execution attempts and no pending, leased or unresolved uncertain session sends. The narrowly linked, confirmed repair below is the only exception for retained historical create uncertainty. It uses the owner's nonblocking setup lock. The permanent signed activation latch prevents preparation after any owner activation, even if owner descriptors are removed. Capacity must remain identical to trusted setup. Ordinary service loads only validate persisted capacity rows; missing or different limits require trusted reconciliation. Each call records its exact request; an uncertain send blocks further preparation until reconciled. Preparation does not complete tasks, start an idle lead or continuously drain events.

### Repair file-literal serialization before owner activation

If an initial create returned a known issue but exact readback refused a provider transformation of an unquoted filename, retain the original task, issue, send, approval, request and deadline. The trusted Python API `installation.workflows.bootstrap.repair_pending_intake(config_file, task_id, create_operation_id, writing_ref, operation_key)` can correct this one preparation failure. It is not a public CLI operation or an ordinary forwarding fallback. Use the existing trusted Writer APIs to prepare, actually review and finalize a new scoped approval first; the public preparation gate remains closed while the send is unsettled.

The new prose may only add inline code spans around existing file/path tokens in prose. Existing code blocks, code spans and link syntax stay byte-for-byte exact. All other text and whitespace, title, code-bearing flag, work units and task identity stay unchanged. The repair derives its issue from the original authenticated send and requires fresh unchanged provider identity, ownership, delegate, revision and body. The exact authenticated snapshot is checked again at the actual update preflight; an intervening edit or delegate removal blocks the mutation. It uses the existing setup lock, current original enrollment and deadline, absent owner/activation history and zero execution attempts. Unknown candidates, changed observations, unrelated uncertainty, invalid approvals and attempted or delivered tasks refuse.

The same canonical task receives one normal `update_prose` intent with the new approval. Its original create remains uncertain and fenced, with its original body, approval and attempts retained; it never becomes a confirmation of the old prose. Only exact provider readback of the new update permits ordinary preparation to continue. A retained uncertain create is excluded from that gate only through its exact repair event and confirmed linked update; other uncertainty still blocks. Retain one repair key: a lost update reply is reconciled by fresh readback on that same key, without another create or blind update retry. No provider readback is normalized, and the old approval gains no new authority.

### Reconcile an applied repair after enrollment expires

Expired enrollment does not permit another mutation or grant execution authority. With new explicit operator authorization, the trusted Python `bootstrap.recover_pending_intake(config_file, task_id, create_operation_id, writing_ref, operation_key, recovery_ref=..., deadline=...)` port can read back an already-sent repair update and record its exact local confirmation. Retain the original repair key, approval, task, provider issue, stores and enrollment. The new recovery reference binds those identities and the original enrollment history to one immutable deadline, at most 300 seconds from authorization. It grants no create, prose, status, preparation or execution permission; no public command exposes it.

This path requires completed unchanged expired enrollment, original physical bindings, no owner or activation history, zero attempts, the exact committed repair and a known quiescent linked update result. Unsent updates, missing identities, revoked enrollment, unrelated uncertainty, changed readback and expired recovery authority refuse. Same-reference replay cannot change the target or deadline. Keep the original uncertain create and its saved failure unchanged.

After exact readback confirms the linked update, ordinary explicit session renewal may recognize that retained create only through its authenticated repair event, original send and confirmed update/send. Recovery itself leaves the old grant expired, configuration and capability unchanged. Normal renewal still requires fresh operator delegation and every existing quiescence and continuity check.

A fixture recovery window is separate from product authority. When explicitly authorized, its trusted `RunLedger.authorize_recovery_window(reference, started_at=..., deadline=..., cleanup_seconds=...)` amendment appends one observation in the original ledger. The original metadata, limits, cumulative request/resource counts and cleanup phase remain intact. The finite window retains a cleanup reserve and permits no automatic extension or counter reset.

## Confirm success

Retain source, wheel and dependency identities; the exact setup/request/approval bytes; command results; canonical task/provider IDs; and independent provider readbacks. Exact submit/enqueue replay uses the original request and operation key and preserves its original effect. A changed payload is a conflict; a new stale-version command is separate from exact replay.

For a DP01 qualification, the accepted observation is one Backlog issue followed by explicit confirmed Todo, matching scoped show/list, and zero product execution attempts or resources. Capture Todo before archiving only the verified run-owned issue and its otherwise-owned disposable project. Development tracking issues cannot substitute for that observation. The [DP01 delivery plan](../plans/deliveries/DP01.md) defines acceptance and the [Test Status Report (TSR)](../test_status_report.md) distinguishes authored controls from real observations.

## If it fails

Stop dependent preparation when enrollment, capacity, writing source, owner state or pending effects refuse. Preserve the original request, operation key, provider outcome and signed history. Do not remove owner files, edit Core rows, reset activation or extend the original deadline to force success. A lost response does not prove rollback. Unknown create/update outcomes require the existing trusted reconciliation before any repeated effect.

An interpreter or validator failure needs the approved runtime restored and verified; it does not authorize replacing the pinned source. Partial, expired or source-mismatched setup needs trusted reconciliation of the retained installation and its effects. This guide provides no automatic migration, authority renewal or destructive reset. Owned cleanup must verify current identity, membership and quiescence; retain an explicit obligation if cleanup cannot be confirmed.

Owner activation and subsequent execution follow their separately authorized procedure in the [independent review How-to Guide (HTG)](../independent_review/how_to_guide.md). They are outside DP01. Reporting an existing scoped event uses the separate command in the [lead reporting How-to Guide (HTG)](../lead_reporting/how_to_guide.md).
