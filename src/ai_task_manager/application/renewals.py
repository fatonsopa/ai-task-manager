"""Current unchanged-graph renewal is serialized by the existing Core transaction."""

from dataclasses import asdict

from ai_task_manager.domains.scheduling import policy
from ai_task_manager.domains.scheduling.models import SchedulingBlocked, WorkRenewal
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import AttemptState as A
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.models import SyncState
from ai_task_manager.storage import attempts, work
from ai_task_manager.storage import scheduling as store


def renew(scheduling, handle, observation_ref, operation_key):
    """Refresh a live unchanged graph; never recreate an expired execution lease.

    The configured resolver performs only prepared local checks here. Network,
    process inspection and complete history preparation must precede this call.
    """

    def action(conn, sid, scope):
        value = scheduling.renewals.verify_renewal(observation_ref)
        if type(value) is not WorkRenewal:
            raise EvidenceRequired("A typed current execution observation is required")
        task = work.get(conn, sid, value.task_id)
        attempt = attempts.get(conn, sid, value.attempt_id)
        scheduling.c.check(task, value.task_version, value.decision_revision)
        now = scheduling.c.clock()
        if (
            value.scope != scope
            or value.session_id != sid
            or value.store_identity != scheduling.c.db.identity(conn)
            or attempt.task_id != task.id
            or attempt.version != value.attempt_version
            or attempt.worker_id != value.worker_id
            or attempt.host != value.host
            or attempt.decision_revision != task.decision_revision
            or attempt.state not in {A.RESERVED, A.STARTING, A.RUNNING}
            or task.human_hold
            or task.delivered_once
            or task.sync_state != SyncState.CONFIRMED
            or task.observed_status not in {S.TODO, S.IN_PROGRESS}
            or not policy.text(value.configuration_digest)
            or not all(
                policy.finite(x)
                for x in (value.observed_at, value.expires_at, value.execution_deadline)
            )
            or not value.observed_at <= now < value.expires_at
            or value.expires_at
            > min(value.execution_deadline, value.observed_at + policy.MAX_RECEIPT_SECONDS)
        ):
            raise EvidenceRequired("Renewal no longer belongs to the live execution owner")
        prior = store.current(conn, task.id)
        if not prior or scheduling._readiness(conn, scope, task, attempt).state != "ready":
            raise SchedulingBlocked("Expired or invalidated authority requires guarded stopping")
        snapshot = value.snapshot
        policy.validate(snapshot, now)

        def definition(s):
            return (
                s.priority,
                sorted(
                    (e.predecessor, e.condition, e.unit or "", e.evidence_refs)
                    for e in s.dependencies
                ),
            )

        if (
            snapshot.scope != scope
            or snapshot.task_id != task.id
            or snapshot.prose_digest != task.prose_digest
            or snapshot.canonical_issue_id != task.issue_id
            or snapshot.source_id != prior["snapshot"].source_id
            or snapshot.previous_graph_version != prior["graph_version"]
            or snapshot.source_revision <= prior["source_revision"]
            or snapshot.observed_at < prior["snapshot"].observed_at
            or snapshot.expires_at < value.expires_at
        ):
            raise SchedulingBlocked("Changed or replayed dependencies require re-evaluation")
        if snapshot.completeness != "complete" or definition(snapshot) != definition(
            prior["snapshot"]
        ):
            store.invalidate(conn, task.id, operation_key, "renewal_definition_changed")
            scheduling.c.changed(
                conn,
                task,
                "scheduling_renewal_fenced",
                {
                    "attempt_id": attempt.id,
                    "observation_ref": observation_ref,
                },
            )
            return scheduling._view(conn, scope, task)
        store.save(conn, snapshot, prior["graph_version"], work.fingerprint(asdict(snapshot)))
        if scheduling._readiness(conn, scope, task, attempt).state != "ready":
            raise SchedulingBlocked("Fresh dependency facts do not authorize work")
        scheduling.c.changed(
            conn,
            task,
            "scheduling_authority_renewed",
            {
                "attempt_id": attempt.id,
                "worker_id": attempt.worker_id,
                "observation_ref": observation_ref,
                "source_revision": snapshot.source_revision,
                "graph_version": prior["graph_version"],
                "expires_at": value.expires_at,
                "execution_deadline": value.execution_deadline,
                "configuration_digest": value.configuration_digest,
            },
        )
        return scheduling._view(conn, scope, task)

    result = scheduling.c.mutation(
        handle,
        operation_key,
        {
            "command": "renew_work_authority",
            "observation_ref": observation_ref,
        },
        action,
    )
    if result["state"] != "ready":
        raise SchedulingBlocked("Changed dependencies fenced this execution; reconcile before work")
    return result
