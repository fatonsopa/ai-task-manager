# Workflow recovery contract

Status: DP05-T01 component contract; cold owner replacement, stopping, successors and retirement remain later DP05 work.
Type: Interface Contract (IC). Sources: [DP05 Implementation Plan (IP)](../plans/deliveries/DP05.md), T01-A1–A3; R03/R14/R15/R27/R36.

## Scope and authority

The provider is the installed operator API, `installation.workflows.recovery.recover`, reached by `aitm workflow recover`. The consumer selects a recorded workflow through its original installation file and service configuration. That JSON file selects existing work; it grants no permission. The original live owner verifies the current enrolled session through its configured observation source. CLI and MCP retain the shared Core policy.

The signed owner-activation record now captures the original installation file identity, workflow file identity and store UUID, and actual owner PID/start identity. The operator verifies those facts with the original authority key and current source/configuration. It opens the authority record read-only and the workflow journal in existing-file mode, without migration or file creation. A missing or changed witness is a reconciliation blocker. Historical rows are not backfilled. Ordinary supported run/resume behavior remains available under its existing authority contract.

## Operations and messages

The operator command is:

```sh
aitm --config <service.json> --installation <installation.json> --json workflow recover --input <request.json>
```

Only `inspect` and live-owner `resume` are implemented in T01. `inspect` returns the recorded workflow/task/plan/key/deadline, attempt identity, current task version/decision/status/prose digest, recorded or pending step names, and `owner_state: live`. It creates no attempt or external mutation. `resume` forwards the original workflow reference to the existing guarded owner. The controller rechecks current decisions and recorded effects before continuing; repeating a completed stage supplies no new execution authority. The `action_availability` result lists `inspect` as callable with `effect_eligibility: no_effects`, and `resume` as callable with `effect_eligibility: requires_workflow_guards`. It lists `stop`, `successor` and `retire` as non-callable with `blocker: not_installed`. Callability describes the available interface; inspection does not grant execution authority or establish that a future effect is eligible.

The closed request function in `installation/workflows/recovery.py` owns operator syntax. The public `workflow_inspect` schema in `application/service/models.py` accepts only `workflow_ref`; its handler verifies current same-session enrollment before and after observation. CLI `workflow inspect` and MCP expose that scoped read. They cannot mint recovery or successor authority. The existing trusted typed `WorkflowService.recover_expired(RecoveryAuthorization)` remains code-only and retains its narrow zero-effect contract.

## Data and validation

An operator request has exactly three fields:

| Field | Required value |
| --- | --- |
| `action` | `inspect`, `resume`, `stop`, `successor` or `retire`; only the first two have installed T01 behavior |
| `workflow_ref` | Nonblank string, 1–256 characters, selecting an existing workflow in the configured scope |
| `operation_key` | Nonblank string, 1–256 characters, equal to that workflow's original operation key |

Unknown fields are refused. The request cannot contain commands, imports, arbitrary paths, proof objects, deadlines or permission claims. The installation bound by the original signed activation must still have its finite future deadline and match its recorded physical path, source and configuration. The selected task, plan and key must match saved history. A copied or rewritten installation cannot replace the signed original.

Example request, using illustrative identifiers:

```json
{"action":"inspect","workflow_ref":"workflow:recorded-id","operation_key":"original-run-key"}
```

The response uses the existing application envelope: `ok`, `outcome`, `data` or `error`, and applicable `exit_code`. Inspection returns facts, not business completion or human acceptance. A successful resume may still return the ordinary blocked/uncertain result of its workflow, including `awaiting_review`.

## Access and failure semantics

Current source, configuration, signed activation, physical stores, scope and original identities must remain valid. The original live owner authenticates its transport and rechecks current enrollment; caller-authored JSON cannot supply that observation. Expired/revoked/foreign enrollment is denied. Missing original history stays protected and is never recreated.

The existing envelope distinguishes invalid input (exit 2), denied evidence/authority (3), unavailable prerequisites (4), unclassified failed operations (5) and uncertain transport/effects (6). Dead, reused or unknown owner observations do not permit a replacement or signal. T01 refuses dependent recovery when the owner is not verified live; T02 supplies the later cold recovery connection. `stop`, `successor` and `retire` are explicit unavailable actions in T01, not successful no-ops.

Local records and external effects remain separate transactions. A missing reply is not rollback. The original key/reference select retained history; they do not authorize a second attempt or an uncertain external resend. No distributed atomicity or exactly-once external execution is claimed.

## Compatibility and verification

Existing workflow rows, `WorkflowStore.read` results, original keys and zero-effect recovery authority retain their meanings. The new `original_key` accessor avoids changing the old read result. New activation metadata is additive; existing activation rows without it remain readable and are not upgraded into recovery permission.

The T01 checks are in `tests/workflows/test_operator_recovery.py`, with affected existing public-schema, startup and ER01–ER10 controls. Installed checks run the stock executable against a real local mechanical owner with authored provider observations. Separate production-composition refusal cases use recorded unexecuted intents. Native-fixture failures at the unchanged protected-source boundary remain failed attempts; the mechanical result does not establish that native path. They establish installed component behavior, not genuine provider/model execution or complete DP05 acceptance. S1's genuine interruption and cold replacement demonstration remains T02. Later host/profile, concurrency/performance and release obligations remain DP07, DP06 and DP14.

## Forward-captured harness stop component

The current T02 source adds the internal `ProcessRuntime(..., recovery_directory=...)` option and `adapters.process.recovery.stop_waiting(journal, attempt_id, check_authority=..., timeout=4)`. Production installation composition does not enable this option yet; the operator `stop` action remains unavailable. This component is a partial T02 increment, not S1 acceptance.

When explicitly enabled by a trusted runtime constructor, launch records a private stop endpoint, secret and worker-source hash before creating the waiting harness. A private inherited file descriptor supplies the secret to that original process. Readiness records only the endpoint's physical identity. The secret stays in the controller journal and inherited input, outside argv, readiness, public stop results and workflow inspection. Future production wiring must protect that journal and endpoint from native/model access and admit a current typed stop authority before using this component. The callback is an internal raising guard, not caller-authored evidence or an execution grant.

The original harness accepts only authenticated `stop` requests bound to its attempt and actual PID. It stops its own process group. Losing the original owner before a work grant leaves this optional harness fenced until its original deadline or a valid stop; it cannot receive a new work grant through the stop endpoint. The default runtime retains its prior behavior. No historical launch is backfilled with a channel.

The cold caller checks original launch, process, source and endpoint observations, then observes the recorded group without recreating child handles or signaling a numeric PID. Unknown inventory, changed/reused identity or missing observations stays uncertain. A successful acknowledgement is not quiescence: a separate full-group observation must establish no live members. The result is one `cold_harness_stop` observation, reused on retry after a fresh absence check. It establishes neither model success nor native, command, collector or merge settlement. No Core capacity or resource is released here. The timeout is a finite observation/request budget of at most four seconds; an individual process inventory has its existing two-second bound. An expired or raising authority guard prevents the next control/settlement step.

Selected checks in `tests/runtime/test_cold_harness_stop.py` use a real killed owner and an original waiting process, with authored provider and authority controls. They cover positive stop, replay, malformed/forged requests, refusal, unknown/reused identity and a separate foreign process. Full cold authority, owner replacement/routing, all-component settlement and genuine installed S1 qualification remain required T02 work.
