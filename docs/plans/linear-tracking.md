# Linear delivery tracking

Every delivery plan, story and task is tracked in the existing [TST development project](https://linear.app/manaferra/project/build-ai-task-manager-20261101t155648z-f7408fef8d55). This map records identity and hierarchy. Read current status from Linear and the owning delivery session; the table is not a second status store.

Follow [Linear delivery tracking](../implementation_plan.md#linear-delivery-tracking) for admission, state changes, evidence and preservation of superseded work. A local planning ID remains stable when its parent changes.

DP01 and its bounded outcome epic DP01-E1 share one record. DP02–DP14 are roadmap records; their detailed stories and tasks are added when those plans are developed.

| Planning ID | Deliverable | Parent record | Linear record |
| --- | --- | --- | --- |
| `DP01` | Install and prepare approved tasks | TST development project | [TST-239](https://linear.app/manaferra/issue/TST-239/dp01-install-and-prepare-approved-tasks) |
| `DP02` | Run and report one non-code task | TST development project | [TST-240](https://linear.app/manaferra/issue/TST-240/dp02-run-and-report-one-non-code-task) |
| `DP03` | Review, commit and merge one code task | TST development project | [TST-241](https://linear.app/manaferra/issue/TST-241/dp03-review-commit-and-merge-one-code-task) |
| `DP04` | Synchronize task changes and human decisions | TST development project | [TST-242](https://linear.app/manaferra/issue/TST-242/dp04-synchronize-task-changes-and-human-decisions) |
| `DP05` | Recover interrupted and expired work | TST development project | [TST-243](https://linear.app/manaferra/issue/TST-243/dp05-recover-interrupted-and-expired-work) |
| `DP06` | Schedule and deliver concurrent tasks | TST development project | [TST-244](https://linear.app/manaferra/issue/TST-244/dp06-schedule-and-deliver-concurrent-tasks) |
| `DP07` | Run complete workflows in Claude Code and Codex CLI | TST development project | [TST-245](https://linear.app/manaferra/issue/TST-245/dp07-run-complete-workflows-in-claude-code-and-codex-cli) |
| `DP08` | Use the task sidebar in supported terminals | TST development project | [TST-246](https://linear.app/manaferra/issue/TST-246/dp08-use-the-task-sidebar-in-supported-terminals) |
| `DP09` | Install and operate the coordination skills | TST development project | [TST-247](https://linear.app/manaferra/issue/TST-247/dp09-install-and-operate-the-coordination-skills) |
| `DP10` | Update, disable and remove the product safely | TST development project | [TST-248](https://linear.app/manaferra/issue/TST-248/dp10-update-disable-and-remove-the-product-safely) |
| `DP11` | Migrate projects with one task-state writer | TST development project | [TST-249](https://linear.app/manaferra/issue/TST-249/dp11-migrate-projects-with-one-task-state-writer) |
| `DP12` | Deliver push, release, hotfix and target synchronization | TST development project | [TST-250](https://linear.app/manaferra/issue/TST-250/dp12-deliver-push-release-hotfix-and-target-synchronization) |
| `DP13` | Repair opted-in product incidents | TST development project | [TST-251](https://linear.app/manaferra/issue/TST-251/dp13-repair-opted-in-product-incidents) |
| `DP14` | Qualify and deliver the complete release | TST development project | [TST-252](https://linear.app/manaferra/issue/TST-252/dp14-qualify-and-deliver-the-complete-release) |
| `DP01-S1` | Establish a trusted isolated installation | [DP01](https://linear.app/manaferra/issue/TST-239/dp01-install-and-prepare-approved-tasks) | [TST-253](https://linear.app/manaferra/issue/TST-253/dp01-s1-establish-a-trusted-isolated-installation) |
| `DP01-S2` | Create one approved task in Backlog | [DP01](https://linear.app/manaferra/issue/TST-239/dp01-install-and-prepare-approved-tasks) | [TST-254](https://linear.app/manaferra/issue/TST-254/dp01-s2-create-one-approved-task-in-backlog) |
| `DP01-S3` | Queue the task and inspect confirmed state safely | [DP01](https://linear.app/manaferra/issue/TST-239/dp01-install-and-prepare-approved-tasks) | [TST-255](https://linear.app/manaferra/issue/TST-255/dp01-s3-queue-the-task-and-inspect-confirmed-state-safely) |
| `DP01-T01` | Reconcile DP01 admission and preserved work | [DP01-S1](https://linear.app/manaferra/issue/TST-253/dp01-s1-establish-a-trusted-isolated-installation) | [TST-256](https://linear.app/manaferra/issue/TST-256/dp01-t01-reconcile-dp01-admission-and-preserved-work) |
| `DP01-T02` | Expose trusted session setup and readiness checks | [DP01-S1](https://linear.app/manaferra/issue/TST-253/dp01-s1-establish-a-trusted-isolated-installation) | [TST-257](https://linear.app/manaferra/issue/TST-257/dp01-t02-expose-trusted-session-setup-and-readiness-checks) |
| `DP01-T03` | Verify installation from an empty project | [DP01-S1](https://linear.app/manaferra/issue/TST-253/dp01-s1-establish-a-trusted-isolated-installation) | [TST-258](https://linear.app/manaferra/issue/TST-258/dp01-t03-verify-installation-from-an-empty-project) |
| `DP01-T04` | Verify exact approval and Backlog submission | [DP01-S2](https://linear.app/manaferra/issue/TST-254/dp01-s2-create-one-approved-task-in-backlog) | [TST-259](https://linear.app/manaferra/issue/TST-259/dp01-t04-verify-exact-approval-and-backlog-submission) |
| `DP01-T05` | Demonstrate real approved Backlog creation | [DP01-S2](https://linear.app/manaferra/issue/TST-254/dp01-s2-create-one-approved-task-in-backlog) | [TST-260](https://linear.app/manaferra/issue/TST-260/dp01-t05-demonstrate-real-approved-backlog-creation) |
| `DP01-T06` | Verify guarded enqueue and scoped inspection | [DP01-S3](https://linear.app/manaferra/issue/TST-255/dp01-s3-queue-the-task-and-inspect-confirmed-state-safely) | [TST-261](https://linear.app/manaferra/issue/TST-261/dp01-t06-verify-guarded-enqueue-and-scoped-inspection) |
| `DP01-T07` | Demonstrate real Todo and settle owned fixtures | [DP01-S3](https://linear.app/manaferra/issue/TST-255/dp01-s3-queue-the-task-and-inspect-confirmed-state-safely) | [TST-262](https://linear.app/manaferra/issue/TST-262/dp01-t07-demonstrate-real-todo-and-settle-owned-fixtures) |
| `DP01-T08` | Close the integrated delivery and hand off DP02 | [DP01-S3](https://linear.app/manaferra/issue/TST-255/dp01-s3-queue-the-task-and-inspect-confirmed-state-safely) | [TST-263](https://linear.app/manaferra/issue/TST-263/dp01-t08-close-the-integrated-delivery-and-hand-off-dp02) |

## DP02 approved hierarchy

DP02 version 1 was approved on 14 November 2026. Current status belongs to Linear and the owning session.

| Planning ID | Parent | Linear record |
| --- | --- | --- |
| `DP02-S1` | `DP02` | [TST-265](https://linear.app/manaferra/issue/TST-265/dp02-s1-prepare-an-executable-isolated-session) |
| `DP02-S2` | `DP02` | [TST-266](https://linear.app/manaferra/issue/TST-266/dp02-s2-execute-and-accept-the-report) |
| `DP02-S3` | `DP02` | [TST-267](https://linear.app/manaferra/issue/TST-267/dp02-s3-report-the-result-and-settle-ownership) |
| `DP02-T01` | `DP02-S1` | [TST-268](https://linear.app/manaferra/issue/TST-268/dp02-t01-resolve-the-normal-execution-prerequisites) |
| `DP02-T02` | `DP02-S1` | [TST-269](https://linear.app/manaferra/issue/TST-269/dp02-t02-prepare-the-fresh-author-and-reviewer-tasks) |
| `DP02-T03` | `DP02-S2` | [TST-270](https://linear.app/manaferra/issue/TST-270/dp02-t03-complete-the-installed-non-code-execution-path) |
| `DP02-T04` | `DP02-S2` | [TST-271](https://linear.app/manaferra/issue/TST-271/dp02-t04-demonstrate-genuine-work-independent-review-and-done) |
| `DP02-T05` | `DP02-S3` | [TST-272](https://linear.app/manaferra/issue/TST-272/dp02-t05-deliver-the-confirmed-result-to-the-lead) |
| `DP02-T06` | `DP02-S3` | [TST-273](https://linear.app/manaferra/issue/TST-273/dp02-t06-close-the-cumulative-delivery-and-hand-off-dp03) |

## DP03 approved hierarchy

DP03 version 1, including all thirty protections, was approved here on 14 November 2026. Native parents and actual identifiers are confirmed; current execution state belongs to Linear and the owning session.

| Planning ID | Parent | Linear record |
| --- | --- | --- |
| `DP03-S1` | `DP03` | [TST-276](https://linear.app/manaferra/issue/TST-276/dp03-s1-admit-an-isolated-code-delivery) |
| `DP03-S2` | `DP03` | [TST-277](https://linear.app/manaferra/issue/TST-277/dp03-s2-deliver-the-reviewed-worker-commit-to-dev) |
| `DP03-S3` | `DP03` | [TST-278](https://linear.app/manaferra/issue/TST-278/dp03-s3-report-and-retire-the-code-delivery) |
| `DP03-T01` | `DP03-S1` | [TST-279](https://linear.app/manaferra/issue/TST-279/dp03-t01-bind-code-delivery-admission-and-trusted-fixture-inputs) |
| `DP03-T02` | `DP03-S1` | [TST-280](https://linear.app/manaferra/issue/TST-280/dp03-t02-prepare-the-author-and-both-code-review-roles) |
| `DP03-T03` | `DP03-S2` | [TST-281](https://linear.app/manaferra/issue/TST-281/dp03-t03-produce-the-checked-worker-commit) |
| `DP03-T04` | `DP03-S2` | [TST-282](https://linear.app/manaferra/issue/TST-282/dp03-t04-review-and-merge-the-exact-candidate-into-dev) |
| `DP03-T05` | `DP03-S3` | [TST-283](https://linear.app/manaferra/issue/TST-283/dp03-t05-report-the-verified-in-dev-result-and-reconcile-replay) |
| `DP03-T06` | `DP03-S3` | [TST-284](https://linear.app/manaferra/issue/TST-284/dp03-t06-retire-owned-resources-and-accept-the-cumulative-handoff) |

## Preserved work

The 13 November 2026 reconciliation preserves original issues and their history, code, branches, failed attempts and acceptance records. Each successor plan lists the older issues to inspect and reuse. Superseded records are archived only after ownership and current activity are checked; archive does not claim completion or discard remaining obligations.

The private reconciliation record is `.local/delivery-learning/20261113-tst-reconciliation-v1/`. It contains the before snapshot, comments, retained-work crosswalk, identity/ownership observations, mutation receipts and final readback. Previously completed issues remain completed. Development tracking changes do not count as product acceptance.

Actual execution remains owned by the delivery session. The human assignee, selected verified Linear app delegate and lead/worker execution owner are separate. Keep all three truthful when an assignment or connection changes.

<a id="dp04-proposed-hierarchy"></a>

## DP04 approved hierarchy

Version 1 at planning commit `4eaf60b` was approved on 15 November 2026: “Approve DP04 version 1 and execute.” All records were read back in Backlog, with their native parent, human assignee and selected application delegate confirmed. The plan and `DP04-E1` share TST-242. This map records identity; current state and criterion/evidence verdicts belong to Linear.

| Planning ID | Parent | Linear record |
| --- | --- | --- |
| `DP04-S1` | `DP04` | [TST-303](https://linear.app/manaferra/issue/TST-303/dp04-s1-reuse-an-existing-issue-safely) |
| `DP04-S2` | `DP04` | [TST-304](https://linear.app/manaferra/issue/TST-304/dp04-s2-see-immediate-changes-and-recover-missed-events) |
| `DP04-S3` | `DP04` | [TST-305](https://linear.app/manaferra/issue/TST-305/dp04-s3-preserve-human-decisions-and-request-a-choice) |
| `DP04-S4` | `DP04` | [TST-306](https://linear.app/manaferra/issue/TST-306/dp04-s4-keep-two-users-isolated-and-deliver-the-increment) |
| `DP04-T01` | `DP04-S1` | [TST-307](https://linear.app/manaferra/issue/TST-307/dp04-t01-connect-trusted-existing-issue-selection-and-intake) |
| `DP04-T02` | `DP04-S1` | [TST-308](https://linear.app/manaferra/issue/TST-308/dp04-t02-demonstrate-existing-intake-and-immediate-confirmed-updates) |
| `DP04-T03` | `DP04-S2` | [TST-309](https://linear.app/manaferra/issue/TST-309/dp04-t03-complete-installed-event-and-polling-connections) |
| `DP04-T04` | `DP04-S2` | [TST-310](https://linear.app/manaferra/issue/TST-310/dp04-t04-demonstrate-signed-synchronization-and-missed-change-recovery) |
| `DP04-T05` | `DP04-S3` | [TST-311](https://linear.app/manaferra/issue/TST-311/dp04-t05-preserve-human-authority-across-task-and-lifecycle-changes) |
| `DP04-T06` | `DP04-S3` | [TST-312](https://linear.app/manaferra/issue/TST-312/dp04-t06-publish-and-resolve-genuine-decision-requests) |
| `DP04-T07` | `DP04-S3` | [TST-313](https://linear.app/manaferra/issue/TST-313/dp04-t07-demonstrate-human-choices-and-decision-only-notifications) |
| `DP04-T08` | `DP04-S4` | [TST-314](https://linear.app/manaferra/issue/TST-314/dp04-t08-demonstrate-genuine-two-user-isolation) |
| `DP04-T09` | `DP04-S4` | [TST-315](https://linear.app/manaferra/issue/TST-315/dp04-t09-accept-the-cumulative-increment-and-hand-off-dp05) |

## DP05 approved hierarchy

Version 1 was approved on 16 November 2026: “Approve DP05 version 1 and execute.” All records were independently read back in Backlog with native parents, human assignment, selected active application delegate and native Type metadata confirmed. DP05 and DP05-E1 share existing TST-243. This map records identity; current state and acceptance evidence belong to Linear. Execution admission follows the approved dependency order.

| Planning ID | Parent | Linear record |
| --- | --- | --- |
| `DP05-S1` | `DP05` | [TST-338](https://linear.app/manaferra/issue/TST-338/dp05-s1-resume-or-stop-after-an-owner-interruption) |
| `DP05-S2` | `DP05` | [TST-339](https://linear.app/manaferra/issue/TST-339/dp05-s2-continue-after-partial-or-uncertain-effects) |
| `DP05-S3` | `DP05` | [TST-340](https://linear.app/manaferra/issue/TST-340/dp05-s3-continue-with-an-explicitly-authorized-successor) |
| `DP05-S4` | `DP05` | [TST-341](https://linear.app/manaferra/issue/TST-341/dp05-s4-settle-retained-obligations-and-report-recovery) |
| `DP05-T01` | `DP05-S1` | [TST-342](https://linear.app/manaferra/issue/TST-342/dp05-t01-connect-the-trusted-installed-recovery-interface) |
| `DP05-T02` | `DP05-S1` | [TST-343](https://linear.app/manaferra/issue/TST-343/dp05-t02-recover-and-settle-an-interrupted-owner-and-worker) |
| `DP05-T03` | `DP05-S2` | [TST-344](https://linear.app/manaferra/issue/TST-344/dp05-t03-reconcile-retained-provider-operations) |
| `DP05-T04` | `DP05-S2` | [TST-345](https://linear.app/manaferra/issue/TST-345/dp05-t04-preserve-partial-code-and-reconcile-publication) |
| `DP05-T05` | `DP05-S2` | [TST-346](https://linear.app/manaferra/issue/TST-346/dp05-t05-demonstrate-installed-partial-effect-recovery) |
| `DP05-T06` | `DP05-S3` | [TST-347](https://linear.app/manaferra/issue/TST-347/dp05-t06-admit-explicitly-authorized-recovery-successors) |
| `DP05-T07` | `DP05-S3` | [TST-348](https://linear.app/manaferra/issue/TST-348/dp05-t07-demonstrate-expiry-and-revoked-predecessor-recovery) |
| `DP05-T08` | `DP05-S4` | [TST-349](https://linear.app/manaferra/issue/TST-349/dp05-t08-recover-eligible-resource-retirement) |
| `DP05-T09` | `DP05-S4` | [TST-350](https://linear.app/manaferra/issue/TST-350/dp05-t09-deliver-the-observed-recovery-report) |
| `DP05-T10` | `DP05-S4` | [TST-351](https://linear.app/manaferra/issue/TST-351/dp05-t10-accept-the-cumulative-recovery-increment-and-hand-off-dp06) |

## DP06 approved hierarchy

Version 1 was approved by the user on 17 November 2026, including DP06-D1, for complete scoped execution. The plan and `DP06-E1` share TST-244. Current acceptance and execution state remain in Linear and the owning delivery records.

| Planning ID | Parent | Linear record |
| --- | --- | --- |
| `DP06-S1` | TST-244 | [TST-415](https://linear.app/manaferra/issue/TST-415/dp06-s1-choose-eligible-work-and-explain-fair-waiting) |
| `DP06-S2` | TST-244 | [TST-416](https://linear.app/manaferra/issue/TST-416/dp06-s2-execute-independent-tasks-safely-in-parallel) |
| `DP06-S3` | TST-244 | [TST-417](https://linear.app/manaferra/issue/TST-417/dp06-s3-deliver-through-contention-within-measured-budgets) |
| `DP06-S4` | TST-244 | [TST-418](https://linear.app/manaferra/issue/TST-418/dp06-s4-complete-the-concurrent-increment-and-supported-operation) |
| `DP06-T01` | TST-415 | [TST-419](https://linear.app/manaferra/issue/TST-419/dp06-t01-enforce-explainable-fair-eligibility) |
| `DP06-T02` | TST-415 | [TST-420](https://linear.app/manaferra/issue/TST-420/dp06-t02-demonstrate-installed-queue-decisions) |
| `DP06-T03` | TST-416 | [TST-421](https://linear.app/manaferra/issue/TST-421/dp06-t03-reserve-finite-conflicting-resources-atomically) |
| `DP06-T04` | TST-416 | [TST-422](https://linear.app/manaferra/issue/TST-422/dp06-t04-connect-concurrent-installed-workflow-ownership) |
| `DP06-T05` | TST-416 | [TST-423](https://linear.app/manaferra/issue/TST-423/dp06-t05-deliver-two-genuine-independent-tasks-concurrently) |
| `DP06-T06` | TST-417 | [TST-424](https://linear.app/manaferra/issue/TST-424/dp06-t06-resolve-merge-contention-and-resource-recovery-gaps) |
| `DP06-T07` | TST-417 | [TST-425](https://linear.app/manaferra/issue/TST-425/dp06-t07-qualify-the-fixed-performance-workloads) |
| `DP06-T08` | TST-418 | [TST-426](https://linear.app/manaferra/issue/TST-426/dp06-t08-demonstrate-dependent-progress-and-interrupted-concurrency) |
| `DP06-T09` | TST-418 | [TST-427](https://linear.app/manaferra/issue/TST-427/dp06-t09-accept-the-cumulative-increment-and-hand-off-dp07) |
