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

## Preserved work

The 13 November 2026 reconciliation preserves original issues and their history, code, branches, failed attempts and acceptance records. Each successor plan lists the older issues to inspect and reuse. Superseded records are archived only after ownership and current activity are checked; archive does not claim completion or discard remaining obligations.

The private reconciliation record is `.local/delivery-learning/20261113-tst-reconciliation-v1/`. It contains the before snapshot, comments, retained-work crosswalk, identity/ownership observations, mutation receipts and final readback. Previously completed issues remain completed. Development tracking changes do not count as product acceptance.

Actual execution remains owned by the delivery session. The human assignee, selected verified Linear app delegate and lead/worker execution owner are separate. Keep all three truthful when an assignment or connection changes.
