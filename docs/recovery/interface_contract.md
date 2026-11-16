# Workflow recovery contract

Status: DP05-T02 source candidate. Cold replacement and owned stop have authored component controls; genuine S1 qualification remains open. Successors and resource retirement remain later DP05 work.
Type: Interface Contract (IC). Sources: [DP05 Implementation Plan (IP)](../plans/deliveries/DP05.md), T01-A1–A3 and T02-A1–A3, parent DP05-S1; R03/R14/R15/R27/R36. The current correction addresses findings DP05-T02-R01, DP05-T02-R02 and DP05-T02-R03 and still requires independent review.

## Scope and authority

The provider is the installed operator API, `installation.workflows.recovery.recover`, reached by `aitm workflow recover`. The consumer selects a recorded workflow through its original installation file and service configuration. That JSON file selects existing work; it grants no permission. The original live owner verifies the current enrolled session through its configured observation source. CLI and MCP retain the shared Core policy.

The signed owner-activation record captures the original installation file identity, workflow file identity and store UUID, and actual owner PID/start identity. New installation activations also bind the original Core, authority, runtime, host and configured provider/dependency/writing journals, keys and session file. The operator verifies these facts with the original authority key and current source/configuration. Cold composition opens current stores in existing-file mode, without initialization or migration. A missing or changed witness is a reconciliation blocker. Historical rows are not backfilled. Ordinary supported run/resume behavior remains available under its existing authority contract.

## Operations and messages

The operator command is:

```sh
aitm --config <service.json> --installation <installation.json> --json workflow recover --input <request.json>
```

`inspect` returns the recorded workflow/task/plan/key/deadline, attempt identity, current task version/decision/status/prose digest, recorded or pending step names, and the verified owner state. It creates no attempt or external mutation. A live-owner `resume` forwards the original workflow reference to the existing guarded owner. For a verified dead owner, `resume` starts a different installed owner process under a signed replacement link in the original authority journal. The original config, installation, descriptor, stores, workflow, operation key, plan and execution deadline remain unchanged. Only that verified replacement lineage may retire the original stale owner socket. The ordinary client follows the signed link to the replacement; no copied configuration or new authority database is involved.

A cold `resume` requires current same-session enrollment and the original unexpired native permission. It may release only a forward-recorded claim belonging to the verified dead controller. A completed stage is reconciled from its original result and exact retained resources; it does not launch another worker. An unresolved stage, changed task decision or uncertain external effect retains its ordinary blocker.

`stop` is available through the operator interface for a verified dead owner with complete forward ownership facts. It records a durable stop intent for the selected workflow and admits a finite, separate stop capability. Cold observation and stop compose the existing Core and signed local sources without constructing a provider client or consulting execution enrollment. They still require every original physical store and signed identity. This capability cannot launch work, refresh enrollment or extend the original execution deadline. An original reservation is resolved through Core's committed operation and event history. Before Core settlement, recovery requires complete observations for native/authentication groups, the waiting harness, commands, collectors, reviewer attempts and any merge obligation. Unknown components retain capacity and protected resources. The stop intent fences later execution of the same workflow, including when a stop reply was lost. Current human changes remain recorded.

A retained author, reviewer or workflow claim may name an earlier owner in the verified signed replacement lineage. Recovery checks that controller's original activation, preserved descriptor, installation and physical source bindings, and confirms that the original process is no longer that owner. It preserves the original controller record; the latest owner does not replace its identity. An unrelated controller remains refused.

The pre-launch path requires an original signed `managed-reservation:` intent recorded before Core reservation. Recovery resolves the exact committed Core operation and event, confirms the dead controller through that same lineage, and holds the original runtime and host journals while checking related component intents, effects, observations and receipts. Any matching fact blocks this pre-launch path. Core independently requires the original reserved attempt with no worker, resource lease or checkpoint. It pauses that same attempt and records `reservation_stopped`; it creates no runtime attempt, managed-owner record, process observation or runtime outcome receipt. An existing runtime claim remains unchanged, and a missing claim stays missing.

The same pre-launch boundary applies to each configured reviewer. Recovery resolves that reviewer's own signed reservation key, task and committed Core operation, then checks its original physical runtime and host journals. It does not create a parent workflow start record for the reviewer. Every reviewer must establish this disposition or complete component-stop evidence before parent settlement.

When the original pending start has no committed reservation, a verified replacement may retry its saved intent under current execution authority. It verifies the original decision, task version, key, deadline and empty reservation history. The first controller's signed pre-reservation witness remains unchanged. A separate signed retry witness records the replacement's actual identity, followed by its ordinary managed-owner record after reservation and runtime claim. A known reservation, related runtime ownership or ambiguous history blocks this retry; it never authorizes another worker for a committed reservation.

A confirmed pre-launch response has `disposition: unlaunched_reservation`, `components_started: false`, the original `attempt_id`, and Boolean `runtime_registered`. The last field describes a controller journal claim, not a launched worker. This path is available only when the original forward witness exists. Older or incomplete reservations without it remain protected; recovery never backfills a witness. Related launch effects, observations, receipts or ambiguous history require the component-stop evidence contract and block this pre-launch disposition.

`action_availability` lists `inspect` and guarded `resume` as callable. A cold inspection also lists `stop` with `effect_eligibility: requires_complete_owned_stop`; a live-owner inspection retains its existing stop-unavailable result. `successor` and `retire` remain unavailable. Callability describes the interface and grants no effect permission.

The closed request function in `installation/workflows/recovery.py` owns operator syntax. The public `workflow_inspect` schema in `application/service/models.py` accepts only `workflow_ref`; its handler verifies current same-session enrollment before and after observation. CLI `workflow inspect` and MCP expose that scoped read. They cannot mint recovery or successor authority. The existing trusted typed `WorkflowService.recover_expired(RecoveryAuthorization)` remains code-only and retains its narrow zero-effect contract.

## Data and validation

An operator request has exactly three fields:

| Field | Required value |
| --- | --- |
| `action` | `inspect`, `resume` or cold `stop`; `successor` and `retire` are recognized but unavailable |
| `workflow_ref` | Nonblank string, 1–256 characters, selecting an existing workflow in the configured scope |
| `operation_key` | Nonblank string, 1–256 characters, equal to that workflow's original operation key |

Unknown fields are refused. The request cannot contain commands, imports, arbitrary paths, proof objects, deadlines or permission claims. The installation must match its recorded physical path, source and configuration. Execution still requires its original finite future deadline. The explicit cold observation/stop port can inspect and settle an expired obligation under separate current stop authority; it cannot execute that expired grant. The selected task, plan and key must match saved history. A copied or rewritten installation cannot replace the signed original.

Example request, using illustrative identifiers:

```json
{"action":"inspect","workflow_ref":"workflow:recorded-id","operation_key":"original-run-key"}
```

The response uses the existing application envelope: `ok`, `outcome`, `data` or `error`, and applicable `exit_code`. Inspection returns facts, not business completion or human acceptance. A successful resume may still return the ordinary blocked/uncertain result of its workflow, including `awaiting_review`.

## Access and failure semantics

Current source, configuration, signed activation, physical stores, scope and original identities must remain valid. The original live owner authenticates its transport and rechecks current enrollment; caller-authored JSON cannot supply that observation. Expired, revoked or foreign enrollment cannot authorize execution. The separate operator stop capability resolves only exact retained obligations. Missing original history stays protected and is never recreated.

The existing envelope distinguishes invalid input (exit 2), denied evidence/authority (3), unavailable prerequisites (4), unclassified failed operations (5) and uncertain transport/effects (6). Unknown owner or component observations block dependent effects. PID reuse never grants signal authority. A cold caller sends no signal to a recorded numeric PID and constructs no replacement child handle. `successor`, `retire` and live-owner `stop` return unavailable results.

Local records and external effects remain separate transactions. A missing reply is not rollback. The original key/reference select retained history; they do not authorize a second attempt or an uncertain external resend. No distributed atomicity or exactly-once external execution is claimed.

## Compatibility and verification

Existing workflow rows, `WorkflowStore.read` results, original keys and zero-effect recovery authority retain their meanings. The new `original_key` accessor avoids changing the old read result. New activation metadata is additive; existing activation rows without it remain readable and are not upgraded into recovery permission.

The T01 checks are in `tests/workflows/test_operator_recovery.py`, with affected existing public-schema, startup and ER01–ER10 controls. Installed checks run the stock executable against a real local mechanical owner with authored provider observations. Separate production-composition refusal cases use recorded unexecuted intents. Native-fixture failures at the unchanged protected-source boundary remain failed attempts; the mechanical result does not establish that native path. They establish installed component behavior, not genuine provider/model execution or complete DP05 acceptance. S1's genuine interruption and cold replacement demonstration remains T02. Later host/profile, concurrency/performance and release obligations remain DP07, DP06 and DP14.

## Forward-captured process and component facts

Production installation composition enables `ProcessRuntime(..., recovery_directory=...)` and consumes `adapters.process.recovery.stop_waiting(journal, attempt_id, check_authority=..., timeout=4)` behind the typed `ColdRecovery` capability. Reviewer composition uses the same retained harness channel. This is a T02 candidate, not S1 acceptance.

Launch records a private stop endpoint, secret and worker-source hash before creating the waiting harness. A private inherited file descriptor supplies the secret to that original process. Readiness records only the endpoint's physical identity. The secret stays in controller storage and inherited input, outside argv, readiness, public stop results and workflow inspection. Controller storage and endpoints remain outside the model's confined workspace and managed tool surface. The callback is an internal guard over the signed current stop capability, not caller-authored evidence or an execution grant.

The original harness accepts only authenticated `stop` requests bound to its attempt and actual PID. It stops its own process group. Losing the original owner before a work grant leaves this optional harness fenced until its original deadline or a valid stop; it cannot receive a new work grant through the stop endpoint. The default runtime retains its prior behavior. No historical launch is backfilled with a channel.

The cold caller checks original launch, process, source and endpoint observations, then observes the recorded group without recreating child handles or signaling a numeric PID. Unknown inventory, changed/reused identity or missing observations stays uncertain. A successful acknowledgement is not quiescence: a separate full-group observation must establish no live members. The result is one `cold_harness_stop` observation, reused on retry after a fresh absence check. It establishes neither model success nor native, command, collector or merge settlement. No Core capacity or resource is released here. The timeout is a finite observation/request budget of at most four seconds; an individual process inventory has its existing two-second bound. An expired or raising authority guard prevents the next control/settlement step.

Managed admission records the actual controller identity before starting components. Signed host facts capture native relays and authentication processes as they are observed. Container watchers retain a separate per-job cleanup signature; a cold caller checks the original signed watch binding, watcher-group absence and cleanup signature before using a lost cleanup reply. Native accounting settles known process stops through its existing controller port without resolving execution permission. Forward reviewer state facts bind each separate runtime and key to its original installation. Pending or uncertain merge ownership remains protected.

Selected checks in `tests/runtime/test_cold_harness_stop.py` cover an actual killed owner and original waiting process, replay, malformed/forged requests, refusal, unknown/reused identity and a foreign process. `tests/workflows/test_cold_operator.py` exercises different actual owners and a stock CLI with explicit authored provider/native ports. Those controls establish component behavior; they are not genuine Claude/account/provider execution. The finite genuine S1 recipe remains in the companion How-to Guide (HTG), pending independent source/effect review and fresh admission.
