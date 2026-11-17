from contextlib import contextmanager
from dataclasses import asdict, replace
from uuid import uuid4

from ai_task_manager.domains.work.errors import (
    CapacityExceeded,
    EvidenceRequired,
    InvalidTransition,
    StaleVersion,
)
from ai_task_manager.domains.work.existing import readback_context
from ai_task_manager.domains.work.models import (
    Attempt,
    Cause,
    SyncState,
    TransitionFacts,
)
from ai_task_manager.domains.work.models import AttemptState as A
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.domains.work.transitions import check_transition
from ai_task_manager.storage import attempts, capacity, issue_claims, scheduling, sessions, work
from ai_task_manager.storage.database import encode

from .existing_issues import ExistingIssues


class Execution:
    def __init__(self, context):
        self.c = context

    @staticmethod
    def require_open(conn, scope, task, publication_context=None):
        try:
            issue_claims.require_open(
                conn, scope, task.issue_id, publication_context=publication_context
            )
        except EvidenceRequired as exc:
            raise InvalidTransition(
                "Delivered or terminal work requires a new linked issue"
            ) from exc

    def _work_lease(self, conn, handle, attempt_id, expected_attempt_version):
        sid, scope = sessions.resolve(conn, handle)
        attempt = attempts.get(conn, sid, attempt_id)
        task = work.get(conn, sid, attempt.task_id)
        if attempt.version != expected_attempt_version:
            raise StaleVersion("Attempt version has changed")
        if (
            attempt.state != A.RUNNING
            or task.human_hold
            or task.delivered_once
            or task.decision_revision != attempt.decision_revision
            or task.observed_status != S.IN_PROGRESS
            or task.sync_state != SyncState.CONFIRMED
        ):
            raise InvalidTransition("Work lease is no longer authorized")
        self.require_open(conn, scope, task)
        self.c.scheduling.require(conn, scope, task, "lease", attempt, session_id=sid)
        return attempt

    def validate_work_lease(self, handle, attempt_id, expected_attempt_version):
        """Recheck authority at a host work boundary, including later human decisions."""
        with self.c.db.transaction() as conn:
            return self._work_lease(conn, handle, attempt_id, expected_attempt_version)

    @contextmanager
    def work_boundary(self, handle, attempt_id, expected_attempt_version, *, inspect=None):
        """Serialize a bounded short effect against other task-authority writers.

        The trusted adapter owns effect intent/reconciliation. This transaction cannot
        roll back a filesystem publication or process spawn, and must never surround
        a whole command, host call, provider request or model turn.
        """
        with self.c.db.transaction() as conn:
            attempt = self._work_lease(conn, handle, attempt_id, expected_attempt_version)
            if inspect is not None:
                inspect(conn)
            yield attempt
            if inspect is not None:
                inspect(conn)

    @contextmanager
    def work_snapshot_boundary(self, handle, attempt_id, expected_attempt_version):
        """Yield immutable authority facts under the existing short-effect lease lock.

        Consumers compare their frozen identities here. Provider, source, resource
        reads and command waits belong outside this transaction.
        """
        with self.c.db.transaction() as conn:
            attempt = self._work_lease(conn, handle, attempt_id, expected_attempt_version)
            _, scope = sessions.resolve(conn, handle)
            task = work.get(conn, attempt.session_id, attempt.task_id)
            yield attempt, task, scope

    @contextmanager
    def worker_commit_boundary(
        self,
        handle,
        task_id,
        attempt_id,
        expected_task_version,
        expected_attempt_version,
        expected_decision_revision,
    ):
        """Release a fixed commit/check child after the owned worker has stopped.

        Only the short gate release belongs inside this transaction. Source,
        resource and process observations remain in the trusted commit service.
        """
        with self.c.db.transaction() as conn:
            sid, scope = sessions.resolve(conn, handle)
            task = work.get(conn, sid, task_id)
            attempt = attempts.get(conn, sid, attempt_id)
            self.c.check(task, expected_task_version, expected_decision_revision)
            if attempt.version != expected_attempt_version:
                raise StaleVersion("Worker commit attempt version changed")
            if (
                not task.code_bearing
                or task.human_hold
                or task.delivered_once
                or task.observed_status != S.IN_PROGRESS
                or task.desired_status != S.IN_PROGRESS
                or task.sync_state != SyncState.CONFIRMED
                or task.execution_state != A.DONE
                or attempt.task_id != task.id
                or attempt.state != A.DONE
                or attempt.decision_revision != task.decision_revision
                or attempts.active(conn, task.id) is not None
            ):
                raise InvalidTransition("Current stopped code worker is required for commit")
            self.require_open(conn, scope, task)
            self.c.scheduling.require(conn, scope, task, "lease", attempt, session_id=sid)
            yield task, attempt, scope

    @contextmanager
    def merge_snapshot_boundary(
        self,
        handle,
        task_id,
        attempt_id,
        expected_task_version,
        expected_attempt_version,
        expected_decision_revision,
        decision_ref,
        publication_context=None,
    ):
        """Authorize only a short merge effect after the task worker has stopped.

        The candidate owner is separately registered by trusted composition. This
        snapshot grants neither work-command permission nor delivered merge status.
        """
        with self.c.db.transaction() as conn:
            yield self._merge_snapshot(
                conn,
                handle,
                task_id,
                attempt_id,
                expected_task_version,
                expected_attempt_version,
                expected_decision_revision,
                decision_ref,
                publication_context,
            )

    def _merge_snapshot(
        self,
        conn,
        handle,
        task_id,
        attempt_id,
        expected_task_version,
        expected_attempt_version,
        expected_decision_revision,
        decision_ref,
        publication_context=None,
    ):
        sid, scope = sessions.resolve(conn, handle)
        task = work.get(conn, sid, task_id)
        self.c.check(task, expected_task_version, expected_decision_revision)
        attempt = attempts.get(conn, sid, attempt_id)
        if attempt.version != expected_attempt_version:
            raise StaleVersion("Completed worker attempt version changed")
        if (
            not task.code_bearing
            or task.human_hold
            or task.delivered_once
            or task.observed_status != S.IN_PROGRESS
            or task.sync_state != SyncState.CONFIRMED
            or attempt.task_id != task.id
            or attempt.state != A.DONE
            or attempts.active(conn, task.id) is not None
        ):
            raise InvalidTransition("Current completed code work and merge authority are required")
        self.require_open(conn, scope, task, publication_context)
        self.c.decision(conn, task, scope, decision_ref, S.IN_DEV)
        return task, attempt, scope

    def reserve(
        self,
        handle,
        task_id,
        host,
        expected_version,
        expected_decision_revision,
        decision_ref,
        operation_key,
        *,
        readback_ref=None,
    ):
        payload = {
            "command": "reserve",
            "task_id": task_id,
            "host": host,
            "version": expected_version,
            "revision": expected_decision_revision,
            "decision_ref": decision_ref,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            self.c.decision(
                conn,
                task,
                scope,
                decision_ref,
                S.IN_PROGRESS,
                human=task.observed_status == S.IN_REVIEW,
            )
            resumable = task.observed_status == S.IN_PROGRESS and task.execution_state in {
                A.FAILED,
                A.PAUSED,
                A.CANCELED,
                A.DONE,
            }
            if (
                task.delivered_once
                or task.sync_state != SyncState.CONFIRMED
                or not task.queued_once
                or (task.observed_status not in {S.TODO, S.ON_HOLD, S.IN_REVIEW} and not resumable)
            ):
                raise InvalidTransition(
                    "Work requires confirmed queue history on an undelivered issue"
                )
            self.require_open(conn, scope, task)
            imported = issue_claims.imported(conn, task.id)
            if imported:
                ExistingIssues(self.c).validate_source(
                    conn,
                    task,
                    scope,
                    readback_ref,
                    readback_context(
                        "start",
                        operation_key,
                        phase="reserve",
                        task_id=task.id,
                        expected_version=expected_version,
                        decision_revision=expected_decision_revision,
                    ),
                )
            if attempts.active(conn, task.id):
                raise CapacityExceeded("Task already owns an active or unresolved attempt")
            graph_version = self.c.scheduling.require(conn, scope, task, "reserve")
            capacity.check(conn, host, scope)
            attempt = Attempt(str(uuid4()), task.id, sid, host, A.RESERVED, task.decision_revision)
            attempts.insert(conn, attempt)
            scheduling.bind_attempt(conn, attempt.id, task.id, graph_version)
            self.c.scheduling.admitted(conn, scope, task)
            task = work.save(conn, replace(task, execution_state=A.RESERVED), task.version)
            self.c.changed(
                conn,
                task,
                "attempt_reserved",
                {
                    "attempt_id": attempt.id,
                    "host": host,
                    **({"readback_ref": readback_ref} if imported else {}),
                },
            )
            return asdict(attempt)

        return attempts.attempt_from(self.c.mutation(handle, operation_key, payload, action))

    def existing_reservation(
        self,
        handle,
        task_id,
        host,
        expected_version,
        decision_revision,
        decision_ref,
        operation_key,
    ):
        """Read an exact committed reservation; absence never creates another attempt."""
        payload = {
            "command": "reserve",
            "task_id": task_id,
            "host": host,
            "version": expected_version,
            "revision": decision_revision,
            "decision_ref": decision_ref,
        }
        with self.c.db.transaction() as conn:
            sid, _ = sessions.resolve(conn, handle)
            recorded = work.replay(conn, sid, operation_key, payload)
            if recorded is None:
                return None
            original = attempts.attempt_from(recorded)
            if (original.task_id, original.session_id, original.host, original.state) != (
                task_id,
                sid,
                host,
                A.RESERVED,
            ):
                raise EvidenceRequired("Reservation operation does not match its execution owner")
            return attempts.get(conn, sid, original.id)

    def settle_unlaunched_reservation(
        self,
        handle,
        attempt_id,
        expected_version,
        expected_decision_revision,
        reservation_key,
        reservation,
        authorization_ref,
        operation_key,
        *,
        inspect,
    ):
        """Settle exact pre-launch intent without claiming that a worker stopped."""
        if (
            not callable(inspect)
            or type(reservation) is not dict
            or set(reservation)
            != {"command", "task_id", "host", "version", "revision", "decision_ref"}
            or reservation["command"] != "reserve"
            or not isinstance(authorization_ref, str)
            or not authorization_ref.startswith("workflow-cold-recovery:")
        ):
            raise EvidenceRequired("Current original-reservation stop boundary required")
        payload = {
            "command": "stop_unlaunched_reservation",
            "attempt_id": attempt_id,
            "version": expected_version,
            "revision": expected_decision_revision,
            "reservation_key": reservation_key,
            "reservation": reservation,
            "authorization_ref": authorization_ref,
        }

        def action(conn, sid, scope):
            attempt = attempts.get(conn, sid, attempt_id)
            task = work.get(conn, sid, attempt.task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            saved = work.replay(conn, sid, reservation_key, reservation)
            if saved is None:
                raise EvidenceRequired("Original committed reservation is unavailable")
            original = attempts.attempt_from(saved)
            if (
                (original.id, original.task_id, original.session_id, original.host, original.state)
                != (attempt.id, attempt.task_id, sid, attempt.host, A.RESERVED)
                or attempt.state != A.RESERVED
                or any(
                    v is not None
                    for v in (attempt.worker_id, attempt.worktree_id, attempt.database_id)
                )
                or conn.execute(
                    "SELECT 1 FROM resource_leases WHERE attempt_id=? LIMIT 1", (attempt.id,)
                ).fetchone()
                or conn.execute(
                    "SELECT 1 FROM checkpoints WHERE attempt_id=? LIMIT 1", (attempt.id,)
                ).fetchone()
            ):
                raise EvidenceRequired("Only the original unlaunched reservation can settle here")
            inspect()
            attempt = replace(attempt, state=A.PAUSED, version=attempt.version + 1)
            attempts.save(conn, attempt)
            task = work.save(conn, replace(task, execution_state=A.PAUSED), task.version)
            self.c.changed(
                conn,
                task,
                "reservation_stopped",
                {
                    "attempt_id": attempt.id,
                    "reservation_key": reservation_key,
                    "authorization_ref": authorization_ref,
                    "components_started": False,
                },
            )
            inspect()
            return asdict(attempt)

        return attempts.attempt_from(self.c.mutation(handle, operation_key, payload, action))

    def start(
        self,
        handle,
        attempt_id,
        expected_version,
        expected_decision_revision,
        startup_ref,
        decision_ref,
        operation_key,
        *,
        readback_ref=None,
    ):
        payload = {
            "command": "start",
            "attempt_id": attempt_id,
            "version": expected_version,
            "revision": expected_decision_revision,
            "startup_ref": startup_ref,
            "decision_ref": decision_ref,
        }

        def action(conn, sid, scope):
            attempt = attempts.get(conn, sid, attempt_id)
            task = work.get(conn, sid, attempt.task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            self.c.decision(
                conn,
                task,
                scope,
                decision_ref,
                S.IN_PROGRESS,
                human=task.observed_status == S.IN_REVIEW,
            )
            if (
                attempt.state != A.RESERVED
                or attempt.decision_revision != task.decision_revision
                or task.delivered_once
                or task.sync_state != SyncState.CONFIRMED
            ):
                raise InvalidTransition("Attempt is not eligible for startup")
            self.require_open(conn, scope, task)
            imported = issue_claims.imported(conn, task.id)
            if imported:
                ExistingIssues(self.c).validate_source(
                    conn,
                    task,
                    scope,
                    readback_ref,
                    readback_context(
                        "start",
                        operation_key,
                        phase="start",
                        task_id=task.id,
                        attempt_id=attempt.id,
                        expected_version=expected_version,
                        decision_revision=expected_decision_revision,
                    ),
                )
            proof = self.c.proof(startup_ref, "worker_start", scope, attempt.id)
            if (
                proof.payload.get("task_id") != task.id
                or proof.payload.get("host") != attempt.host
                or not proof.payload.get("worker_id")
            ):
                raise EvidenceRequired("Trusted startup must bind task, host and worker identity")
            cause = {
                S.TODO: Cause.START,
                S.IN_PROGRESS: Cause.START,
                S.ON_HOLD: Cause.RESUME,
                S.IN_REVIEW: Cause.REVIEW_DECISION,
            }.get(task.observed_status)
            if cause is None:
                raise InvalidTransition("Issue is not eligible for worker startup")
            check_transition(
                task.observed_status,
                S.IN_PROGRESS,
                cause,
                TransitionFacts(authorized=True, queued=task.queued_once, worker_started=True),
            )
            self.c.scheduling.require(conn, scope, task, "start", attempt)
            attempt = replace(
                attempt,
                state=A.STARTING,
                worker_id=proof.payload["worker_id"],
                version=attempt.version + 1,
            )
            attempts.save(conn, attempt)
            task = work.save(
                conn,
                replace(
                    task,
                    desired_status=S.IN_PROGRESS,
                    sync_state=SyncState.PENDING,
                    execution_state=A.STARTING,
                ),
                task.version,
            )
            self.c.changed(
                conn,
                task,
                "worker_started",
                {
                    "attempt_id": attempt.id,
                    "worker_id": attempt.worker_id,
                    "startup_ref": startup_ref,
                    **({"readback_ref": readback_ref} if imported else {}),
                },
                "set_status",
            )
            return asdict(attempt)

        return attempts.attempt_from(self.c.mutation(handle, operation_key, payload, action))

    def running(
        self,
        handle,
        attempt_id,
        expected_version,
        expected_decision_revision,
        resources_ref,
        operation_key,
    ):
        payload = {
            "command": "running",
            "attempt_id": attempt_id,
            "version": expected_version,
            "revision": expected_decision_revision,
            "resources_ref": resources_ref,
        }

        def action(conn, sid, scope):
            attempt = attempts.get(conn, sid, attempt_id)
            task = work.get(conn, sid, attempt.task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            if (
                task.observed_status != S.IN_PROGRESS
                or task.sync_state != SyncState.CONFIRMED
                or task.human_hold
                or task.delivered_once
                or attempt.state != A.STARTING
                or attempt.decision_revision != task.decision_revision
            ):
                raise InvalidTransition(
                    "Running requires confirmed In Progress and current ownership"
                )
            self.require_open(conn, scope, task)
            self.c.scheduling.require(conn, scope, task, "running", attempt)
            proof = self.c.proof(resources_ref, "resources_ready", scope, attempt.id)
            required = {
                "task_id": task.id,
                "host": attempt.host,
                "worker_id": attempt.worker_id,
                "ready": True,
            }
            if (
                any(proof.payload.get(k) != v for k, v in required.items())
                or proof.payload.get("ready") is not True
                or not proof.payload.get("worktree_id")
                or not proof.payload.get("database_id")
            ):
                raise EvidenceRequired("Owned worktree and database must both be ready")
            for kind in ("worktree", "database"):
                resource_id = proof.payload[f"{kind}_id"]
                row = conn.execute(
                    "SELECT attempt_id FROM resource_leases WHERE kind=? AND resource_id=?",
                    (kind, resource_id),
                ).fetchone()
                if row and row[0] != attempt.id:
                    raise EvidenceRequired("Resource is already owned by another attempt")
                conn.execute(
                    "INSERT OR IGNORE INTO resource_leases VALUES(?,?,?)",
                    (kind, resource_id, attempt.id),
                )
            attempt = replace(
                attempt,
                state=A.RUNNING,
                worktree_id=proof.payload["worktree_id"],
                database_id=proof.payload["database_id"],
                version=attempt.version + 1,
            )
            attempts.save(conn, attempt)
            task = work.save(conn, replace(task, execution_state=A.RUNNING), task.version)
            self.c.changed(
                conn,
                task,
                "work_lease_granted",
                {"attempt_id": attempt.id, "resources_ref": resources_ref},
            )
            return asdict(attempt)

        return attempts.attempt_from(self.c.mutation(handle, operation_key, payload, action))

    def checkpoint(self, handle, attempt_id, expected_attempt_version, kind, data, operation_key):
        payload = {
            "command": "checkpoint",
            "attempt_id": attempt_id,
            "version": expected_attempt_version,
            "kind": kind,
            "data": data,
        }

        def action(conn, sid, scope):
            attempt = attempts.get(conn, sid, attempt_id)
            if attempt.version != expected_attempt_version:
                raise StaleVersion("Attempt version has changed")
            # These records describe reports, never trusted liveness or completion.
            checkpoint_id = str(uuid4())
            conn.execute(
                "INSERT INTO checkpoints VALUES(?,?,?,?)",
                (checkpoint_id, attempt.id, kind, encode(data)),
            )
            self.c.changed(
                conn,
                work.get(conn, sid, attempt.task_id),
                "worker_reported_progress",
                {
                    "attempt_id": attempt.id,
                    "checkpoint_id": checkpoint_id,
                    "kind": kind,
                },
            )
            return {"checkpoint_id": checkpoint_id}

        return self.c.mutation(handle, operation_key, payload, action)["checkpoint_id"]

    def settle(
        self,
        handle,
        attempt_id,
        expected_version,
        expected_decision_revision,
        outcome_ref,
        operation_key,
    ):
        payload = {
            "command": "settle",
            "attempt_id": attempt_id,
            "version": expected_version,
            "revision": expected_decision_revision,
            "outcome_ref": outcome_ref,
        }

        def action(conn, sid, scope):
            attempt = attempts.get(conn, sid, attempt_id)
            task = work.get(conn, sid, attempt.task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            proof = self.c.proof(outcome_ref, "attempt_outcome", scope, attempt.id)
            try:
                state = A(proof.payload.get("state"))
            except ValueError as exc:
                raise EvidenceRequired("Unknown execution outcome") from exc
            if state not in {A.DONE, A.PAUSED, A.FAILED, A.CANCELED, A.UNKNOWN}:
                raise InvalidTransition("Outcome is not terminal, paused or unresolved")
            if (
                proof.payload.get("task_id") != task.id
                or proof.payload.get("host") != attempt.host
                or proof.payload.get("worker_id") != attempt.worker_id
            ):
                raise EvidenceRequired("Outcome belongs to another execution owner")
            if state != A.UNKNOWN and proof.payload.get("quiescent") is not True:
                raise EvidenceRequired("Capacity remains owned until worker quiescence is verified")
            if attempt.state in {A.DONE, A.FAILED, A.CANCELED, A.PAUSED}:
                raise InvalidTransition(
                    "Attempt is already settled; retain it and create a new attempt"
                )
            attempt = replace(attempt, state=state, version=attempt.version + 1)
            attempts.save(conn, attempt)
            if state != A.UNKNOWN:
                conn.execute("DELETE FROM resource_leases WHERE attempt_id=?", (attempt.id,))
            task = work.save(conn, replace(task, execution_state=state), task.version)
            self.c.changed(
                conn,
                task,
                "attempt_outcome_recorded",
                {
                    "attempt_id": attempt.id,
                    "outcome": state,
                    "outcome_ref": outcome_ref,
                },
            )
            return asdict(attempt)

        return attempts.attempt_from(self.c.mutation(handle, operation_key, payload, action))

    def complete_unit(
        self,
        handle,
        task_id,
        unit,
        expected_version,
        expected_decision_revision,
        proof_ref,
        operation_key,
    ):
        payload = {
            "command": "complete_unit",
            "task_id": task_id,
            "unit": unit,
            "version": expected_version,
            "revision": expected_decision_revision,
            "proof_ref": proof_ref,
        }

        def action(conn, sid, scope):
            task = work.get(conn, sid, task_id)
            self.c.check(task, expected_version, expected_decision_revision)
            if (
                task.delivered_once
                or task.human_hold
                or task.sync_state != SyncState.CONFIRMED
                or task.observed_status not in {S.IN_PROGRESS, S.IN_REVIEW}
            ):
                raise InvalidTransition("Further work on delivered or held tasks is not admitted")
            proof = self.c.proof(proof_ref, "work_unit_complete", scope, task.id)
            if (
                unit not in task.required_work
                or proof.payload.get("unit") != unit
                or proof.payload.get("prose_digest") != task.prose_digest
            ):
                raise EvidenceRequired("Completion must match a declared unit and approved scope")
            completed = tuple(x for x in task.required_work if x in {*task.completed_work, unit})
            task = work.save(conn, replace(task, completed_work=completed), task.version)
            self.c.changed(conn, task, "work_unit_verified", {"unit": unit, "proof_ref": proof_ref})
            return asdict(task)

        return self.c.task_mutation(handle, operation_key, payload, action)
