# AI task manager development and testing status

Reporting period: 2 November 2026, 02:30–04:57 UTC.
Build: Main 9a9599be; the latest complete backend run tested e713d864 before two later component integrations.
Environment: macOS arm64, Python 3.13.13; separately identified Linux-container checks.
Status: Implementation continues. The tested backend passes its mechanical suite; full product acceptance remains incomplete.

## Progress and results

The configured Mac backend passed 2,490 tests with zero failures, errors or skips. That result belongs to e713d864. Main subsequently integrated the local Linear test driver and isolated AI Index adapter contracts, each with passing focused checks. Their combined main revision has not yet had a complete backend run.

| Scope and source | Observed result | Meaning and limits |
| --- | --- | --- |
| Complete Mac backend, e713d864, tree ac3ad93388eb760c1532c3085b6e761f81a86713 | 2,490 passed; zero failures, errors or skips. Pytest took 558.69 seconds; the wrapper took 560.80 seconds within its 840-second bound. All 667 frozen source/dependency hashes matched. | Mac mechanical evidence on this exact source. The retained `forkpty` deprecation warning did not fail the run. This is not live Linear, selected-model, skill, migration or release acceptance. |
| Actual container cases in that run | All six declared cases passed. Seven unique owned container IDs had cleanup receipts and were independently confirmed absent. The outer process group was stopped and reaped. | These are lightweight Linux-container fixtures. Outer process exit does not substitute for nested resource evidence or native Linux integration. |
| Host startup repairs, e11638ab and c608534b, integrated in e713d864 | The earlier fence/cache repair passed 374 focused checks with six explicitly unselected container cases skipped. The native child-exit repair passed 14 focused checks and the later complete suite. | Counts overlap. Current metadata and an existing kernel exit watch are checked when startup proof is consumed. The lock covers that point check; it does not make the entire Core commit atomic with an external process. |
| Actual native child-exit regression | The retained red case admitted one worker and confirmed provider In Progress after the native child exited while its relay remained stopped. The repaired case recorded zero worker starts and zero provider confirmations. Original child/relay exits and owned recovery were observed separately. | The earlier setup failures and failed admission oracle remain recorded. The fix is Mac-only; native exit monitoring explicitly refuses unsupported platforms. |
| Local Linear driver, 7a59b22c, integrated as 4659330f | 88 passed; zero failures, errors or skips in 22.31 seconds. All original 46 case IDs and 23 test-function bodies were preserved; 42 additive cases exercised the repairs. Owned processes stopped. | Candidate UUID reservations, durable cleanup actions and cumulative cleanup reserves were tested locally. All 18 actual live Linear cases remain not run. |
| Isolated AI Index adapter contracts, cda5b2a7, integrated as 9a9599be | One collection found 123 frozen cases; one focused run passed all 123 with zero failures, errors or skips in 3.03 seconds. The owned temporary tree was inventoried and removed. | F0 typed recording contracts only. There is no configured real foreground runner, shared authority-boundary integration or actual Git/application/provider/host acceptance. |
| Merge integration documents, 158fcaf9 | The expanded Decision Record (DR) and Test Specification (TS) have checked deliveries and retain their source/case mappings. They remain proposed integration design and unrun higher-tier cases. | The audit records 646 captured paths and 396 source-linked rows; 358 paths still lack a contract row. Other partially reviewed and inherited static rows remain explicit. This is not semantic closure of the whole repository. |
| Task and documentation maintenance | Two active task descriptions were checked and updated to remove retired overall-limit claims. Obsolete host-task document links were replaced with maintained paths. Exact provider title/body readback preceded the next worker start. | Terminal-newline mismatches and the lead's earlier task-type routing failures remain recorded. A provider representation is separately reviewed; global exact-readback controls were not relaxed. |
| Coordination skill | The earlier candidate passed 56 mechanical checks. Retained behavior observations remain 0/3 and the host matrix remains 0/6. | The current Builder renderer has a scoped structure pass; the generated skill still needs repair and fresh behavior evidence. |

The callback review added a specific uncertainty rule before F0 execution: a thrown exception can follow a saved downstream effect. The adapter therefore retains uncertainty after exceptions, including `AccessDenied`; a confirmed no-effect rejection requires a matching trusted negative receipt. The effect-then-exception control passed in the focused run.

The earlier 2,440-test Mac backend result on a3d0aca remains historical, as do the 29 Linux store-guard checks, seven final policy-preservation checks and the older 1,790-test Mac/Linux baselines. Their counts are not added to the results above. The original schema amendment retained all 31 original invocation rows and 123 earlier events; current continuations preserve the same run and accounting.

Earlier admission failures, the copied-store reproduction, late startup failures and the failed actual Claude component pilot remain attached to their sources. That pilot edited its fixture and passed an isolated container test, but failed the frozen model-stability requirement after the observed model changed. Successful effects do not make that pilot pass. Lead verification/helper failures and their corrections remain in the private records.

## Coverage and deviations

The global inventory remains 38 requirements, 248 authored scenarios and fourteen workflows. Full-product verdicts remain incomplete. Local test counts, F0 groups and authored driver controls are not a coverage percentage for that inventory.

Every selected profile requires separate actual evidence: Claude Code with `claude-opus-5-5` and `claude-sonnet-5-5`, and Codex CLI with `gpt-6.1-sol` and `gpt-6-astra`, all at `high` effort. Fable is excluded. All four acceptance profiles remain not run. Use `claude_roni`; the original `claude` account remains unavailable.

Four read-only bootstrap Linear schema/metadata queries confirmed the expected account scope and inspected actual input types. They created no fixtures or webhooks and are not product acceptance. In particular, default schema introspection omitted a deprecated project operation; lifecycle selection still needs source review before live effects. The earlier live-preflight invocation missed its deadline and remains failed despite its useful retained packet.

The original run began on 1 November at 15:56:48 UTC. The user lifted overall spending, elapsed-time and retry ceilings. Finite reviewed batches, per-invocation deadlines, charge reservations and measured concurrency remain required. Recorded incremental external charges remain zero. Historical API-equivalent estimates of USD 0.0829696 for the failed Claude component session and USD 0.0266295 for a separate earlier pilot are estimates, not bills. Paid overage, usage credits and automatic reload remain disabled by user confirmation.

## Blockers and risks

* Normal Codex-home preparation still rejects a nonempty ancestor ACL. A Mac descriptor-based proposal would permit a completely inspected DENY-only list while preserving owner/mode/path checks and refusing grants or unknown inspection. Implementation is now assigned; its focused checks have not run. No ACL repair pass, normal permission change or successful account startup is claimed.
* Final launch accounting/admission at the actual process boundary, native configuration conformance, diagnostic bootstrap, output privacy, and actual host selection/delivery/follow-up observations still need completion. The lookup/admission API and process fixtures do not establish those behaviors.
* The local webhook receiver still needs one total accept-to-response deadline covering headers, body, adapter wait and output. A response timeout does not cancel SQLite work. The new receiver assignment must preserve capacity until actual underlying work stops. No public tunnel has been admitted.
* All 18 live Linear cases remain unrun, and the second Linear identity remains unavailable. That identity blocks live two-user acceptance; independent work continues.
* Real Git publication, protected foreground lifetime, application database/resource ownership, callback integration, obsolete-writer retirement and clean cutover remain pending. The working merge route is the baseline; F0 does not replace or certify it. Higher-tier fixtures need their own reviewed configuration, process and resource boundaries.
* Dependency scheduling, recovery/status integration, explicit human acceptance, the native terminal sidebar, coordination-skill repair, installation/removal, continuous improvement, second-project portability and release checks remain incomplete. The browser sidebar design is approved; native interaction is not yet accepted.
* Thirty-six inherited repository-wide lint diagnostics remain unresolved. Scoped lint passes do not establish a clean repository-wide result.

The current recorded AI Index audit source remains 44a91116d73d0a3ac279aa3b064d373000356477. The original formal baseline remains historical. The application checkout at 15992e2 is an ancestor of the audit source, not a newer replacement. Source-only semantic review continues; no broad audit closure or actual merge-effect bound is claimed.

## Next work

Complete and review the Mac ACL classifier and the receiver deadline controls, then run their exact frozen focused cases. Continue the source-only integration audit and freeze the next isolated Git/process proposal before admitting effects. Preserve command names, arguments, checks, foreground merge flow, result meanings and the route-owned database lifecycle.

Finish the remaining host interfaces before separately reviewed installed-host runs. Rebuild the coordination skill through the current Skill Builder when those interfaces are ready, then collect actual behavior evidence on the four selected profiles. Continue other work in dependency order and retain every attempt.

T34 remains in its normal dependency order before packaging. Keep prose, editable/rendered diagrams and screenshots under `docs/`; operational material moves only through the planned responsibility/consumer audit. No structural moves occurred in this period.

No code has been pushed, normal-session activation has not occurred and the application checkout remains unchanged. The required future How-to Guide (HTG), `migrate.md`, must accompany tested clean cutover rather than invented migration commands.

## Sources

The Test Plan (TP), [test_plan.md](test_plan.md), owns entry and exit criteria. The Implementation Plan (IP), [implementation_plan.md](implementation_plan.md), owns dependencies. The Spec Driven Development document (SDD), [spec_driven_development.md](spec_driven_development.md), owns required behavior. The integration Decision Record (DR), [merge/decision_record.md](merge/decision_record.md), and Test Specification (TS), [merge/test_specification.md](merge/test_specification.md), explain the proposed merge boundaries and unrun cases. See the [execution policy](config/execution-policy.json) and [component evidence summary](HANDOFF-CHECKS.json).

Private run 20261101T155648Z retains exact source trees, raw JUnit/logs, owned container IDs and cleanup, native exit observations, writer receipts, failed attempts and original accounting. This period's source snapshot retains the previous report. All results remain bound to their actual source, environment and scope.
