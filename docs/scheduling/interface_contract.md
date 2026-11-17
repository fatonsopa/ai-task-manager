# Queue observation and selection contract

Type: Interface Contract (IC). Status: implemented DP06 queue-read boundary; independent task/story acceptance and genuine execution evidence remain separately scoped. Sources: R07/R11/R15/R27, the approved [Implementation Plan (IP)](../plans/deliveries/DP06.md), `application/service/models.py`, `application/service/queries.py`, `application/scheduling.py` and the native lead composition under `src/ai_task_manager/`.

## Scope and authority

The configured application service provides queue observations. CLI, MCP and the ordinary managed lead consume the same scoped Core facts. Service schemas own request syntax; the implementation's typed projection owns response semantics. This contract describes observation and selection, not a second scheduler, new public scheduling command or permission to execute a model-selected task.

The installed dependency integration supplies current complete provider observations. Core alone decides readiness, ordering and admission. The operator's trusted installed plan selects the permitted workflow, resources and target; models cannot supply replacement ownership, commands, proofs or authority.

## Operations and messages

| Consumer | Operation | Input and result |
| --- | --- | --- |
| CLI | `aitm --config CONFIG --json task list` | No request fields. Returns the current session's visible task projections in admission order, followed by other visible tasks. |
| CLI | `aitm --config CONFIG --json task show --input REQUEST` | Exactly `task_id`, a nonempty opaque string up to 256 characters. Returns one authorized task projection. |
| Application service / MCP | `task_list`, `task_show` | The same closed input schemas and structured result envelopes as CLI. Unknown request properties are refused. |
| Ordinary managed lead | `task_list`, `task_show` | These existing read operations are selected from the sealed lead installation before native tool negotiation. Native permission, active account/model/startup, current provider enrollment and gateway scope remain required. |
| Event-only reporting lead | Existing sealed event operations | A configuration with `reporting_ref` does not expose task reads. It cannot become an ordinary queue-reading lead by supplying another task identifier. |
| Trusted operator | Existing `workflow_run` / `workflow run` | Exactly `task_id`, `plan_ref` and `operation_key`. Rechecks admission and starts only through the installed workflow. Lead selection is neither a reservation nor worker-start authority. |

The success envelope contains `ok: true`, `outcome` and `data`. A list also reports `quarantined_count`; foreign or quarantined prose is excluded. A `task_show` request for another scope is denied. Task and workflow IDs are opaque; titles and display identifiers do not grant access.

## Data and validation

The `scheduling` object accompanies the task's actual `observed_status`, `sync_state`, `human_hold` and execution facts. Its fields are observations, not caller-set parameters.

| Field | Meaning |
| --- | --- |
| `state` | Dependency readiness: `ready`, `blocked` or `unknown`. This does not reserve capacity. |
| `blockers` | Dependency/source blockers with a machine code, authorized dependency identifier or null, and release condition. Foreign predecessor identifiers are not disclosed. |
| `task_version`, `graph_version`, `source_revision` | Bind the decision to its task and dependency observation. Unknown source revisions remain null. |
| `priority`, `queue_sequence`, `expires_at` | Current declared priority, original explicit queue order and receipt expiry. Missing observations/order remain null. |
| `selection_state` | `ready` for the first selection-eligible task, `waiting` for another eligible queued task, `blocked` when selection is unavailable, or the recorded active attempt state. |
| `eligible_since`, `wait_seconds` | Start of the current continuous eligibility epoch and nonnegative elapsed wait, using the configured Core clock in seconds. Both return null outside a current eligible waiting epoch. Ordinary installations use the same clock basis as dependency expiry. |
| `bypasses` | Nonnegative count of committed later-queued higher-priority admissions during that epoch. Failed capacity/claim attempts and replay do not increment it. |
| `fair_turn_due` | True when a currently eligible task has at least four counted bypasses. The oldest qualifying waiter has precedence among promoted tasks. |
| `waiting_reason` | `fair_turn`, `priority_order` or null. Read it with readiness, hold, synchronization and ownership facts; it is not a complete independent authorization verdict. |
| `admission_requires_capacity` | True: selection remains subject to atomic capacity and other current admission checks. |

Routine observations and restart preserve a still-valid waiting epoch. Expiry, dependency loss, changed approved work or a current human hold ends it. Higher priority does not preempt an active attempt. A failed/canceled/held/missing/foreign or ambiguous predecessor is not success; only the declared verified unit, merge or complete-work milestone can release its dependent task.

## Access and failure semantics

Every call requires the sealed configuration and currently authorized session. Native queue reads additionally require the ordinary lead's current role, permission, source/profile/account and configured gateway. Worker and reviewer advertised tools retain their existing set. Queue reads grant no file, command, worker-start or authority-minting capability. Event-only reporting remains confined to its original event and receipts.

Reads do not start workers, reserve capacity, advance wait counters or change provider workflow status. Selection is not atomic with a later operator request: the workflow rechecks all current guards. Provider/network observation and process/resource effects occur outside Core's short admission transaction.

Public failure categories and CLI exit codes remain those in `application/service/models.py`: invalid input 2, denied 3, blocked 4, failed 5 and uncertain 6. A lost run response may follow an effect; preserve the original key/reference and use supported reconciliation. Do not infer rollback or exactly-once external execution from a local query or idempotent reservation.

## Compatibility and verification

Queue fields are additive response data. Existing closed request schemas, task statuses and run/resume keys remain unchanged. Callers can ignore added response fields; selection uses current task/source observations rather than cached titles or row positions. Native launch and authenticated startup verify the exact installer-selected tool set; missing or extra tools still refuse.

`tests/scheduling/test_installed_queue.py` checks literal installed CLI and real MCP stdio agreement, blocked predecessor/hold handling, signed authored milestone release and four-admission fair ordering against Core admission. `tests/native_installation/test_lead_queue_reads.py` checks the configured lead boundary, malformed/foreign/worker-action denials and cross-role negotiation. These controlled cases retain authored provenance. Genuine lead selection, actual worker start, concurrent delivery and performance require their separately bound observations; neither schema conformance nor passing controls establishes those outcomes.
