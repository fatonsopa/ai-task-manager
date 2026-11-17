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
| Trusted operator | Existing `workflow_run` / `workflow run` | Exactly `task_id`, `plan_ref` and `operation_key`. CLI `workflow run` uses `--config` and `--input` through the retained owner; `--installation` belongs to the dedicated owner, lead-report and recovery commands. Rechecks admission and starts only through the installed workflow. Lead selection is neither a reservation nor worker-start authority. |

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

### Declared resource admission

The trusted workflow installation may include `resource_locks`: an explicit array of `[kind, name]` pairs, where `kind` is `migration`, `test` or `resource`. An empty array is a known empty lock set; an absent field is unknown and cannot authorize a new admission. The sealed `files` array supplies literal repository-relative file claims. File/directory path collisions conflict; this interface adds no wildcard expansion. The installer binds the physical Git common directory, task, approved prose, current decision revision, original finite deadline and sealed source files. Models cannot provide these declarations through `workflow run`, `worker start`, CLI or MCP request fields.

Core claims the task, finite host/project capacity and declared resources in its existing admission transaction. Claims use the existing resource leases. Competing file, migration and test claims are checked within the same physical repository; named `resource` locks also conflict across repositories and projects on the shared store. A conflict returns the generic occupied-resource cause without another owner's identity or prose. An occupied queue head waits while compatible independent work can advance. A rejected claim or exact-key replay does not increment committed fairness counters.

Trusted setup records validated resource facts in the existing Core event stream. Ordinary service and lead readers can consume that task-scoped projection while checking its task/prose/revision, original deadline and sealed-file witnesses. Public event payloads report only that resources were declared; repository identity and sealed-file witnesses remain in the private Core audit record. That observation grants no execution: reservation and startup require the currently configured live declaration source and the exact atomic claim. New queue causes are `resource_conflict` for occupied resources and `resource_declaration_unavailable` for missing or stale facts. Provider workflow status is preserved.

The admission lease remains owned during active or unknown execution. Confirmed quiescent execution settlement releases its admission claims; existing physical resource/check/merge consumer controls continue to govern later use and retirement. An active legacy attempt without the new declaration marker is unresolved ownership, not a known empty declaration. It blocks competing admission, and an original-key reservation replay alone cannot launch a replacement. The supported original unlaunched-reservation stop may release its logical marker only after its existing independent zero-effect observations pass.

Core schema 6 reuses the existing forward-writer fence. Migration retains the original before-image, operational rows, unknown attempts, keys and physical resource facts, and replaces the writer epoch atomically. Earlier fresh openers and preopened connections cannot write through the new fence. No old declaration is invented. Legacy plans without `resource_locks` retain their original digest and completed replay history; new work needs a current complete trusted installation. Do not edit an enrolled installation or renew old authority to manufacture compatibility.

## Access and failure semantics

Every call requires the sealed configuration and currently authorized session. Native queue reads additionally require the ordinary lead's current role, permission, source/profile/account and configured gateway. Worker and reviewer advertised tools retain their existing set. Queue reads grant no file, command, worker-start or authority-minting capability. Event-only reporting remains confined to its original event and receipts.

Reads do not start workers, reserve capacity, advance wait counters or change provider workflow status. Selection is not atomic with a later operator request: the workflow rechecks all current guards. Provider/network observation and process/resource effects occur outside Core's short admission transaction.

Public failure categories and CLI exit codes remain those in `application/service/models.py`: invalid input 2, denied 3, blocked 4, failed 5 and uncertain 6. A lost run response may follow an effect; preserve the original key/reference and use supported reconciliation. Do not infer rollback or exactly-once external execution from a local query or idempotent reservation.

## Compatibility and verification

Queue fields are additive response data. Existing closed request schemas, task statuses and run/resume keys remain unchanged. Callers can ignore added response fields; selection uses current task/source observations rather than cached titles or row positions. Native launch and authenticated startup verify the exact installer-selected tool set; missing or extra tools still refuse.

`tests/scheduling/test_installed_queue.py` checks literal installed CLI and real MCP stdio agreement, blocked predecessor/hold handling, signed authored milestone release and four-admission fair ordering against Core admission. `tests/native_installation/test_lead_queue_reads.py` checks the configured lead boundary, malformed/foreign/worker-action denials and cross-role negotiation. These controlled cases retain authored provenance. Genuine lead selection, actual worker start, concurrent delivery and performance require their separately bound observations; neither schema conformance nor passing controls establishes those outcomes.
