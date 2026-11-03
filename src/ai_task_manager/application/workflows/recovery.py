"""Trusted recovery creates fresh intent; it never resumes an expired effect."""

import math
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, ValidationError
from ai_task_manager.domains.work.models import IssueStatus, Scope
from ai_task_manager.storage import issue_claims, sessions, work

from .models import WorkflowPlan


@dataclass(frozen=True)
class RecoveryAuthorization:
    """One explicit operator authorization, supplied by trusted composition code."""

    reference: str
    scope: Scope
    predecessor: str
    task_id: str
    task_version: int
    decision_revision: int
    plan_ref: str
    plan_digest: str
    operation_key: str
    expires_at: float

    def __post_init__(self):
        if type(self.scope) is not Scope or any(
            type(value) is not str or not value.strip() or len(value) > 256
            for value in (
                self.reference,
                self.predecessor,
                self.task_id,
                self.plan_ref,
                self.plan_digest,
                self.operation_key,
            )
        ):
            raise ValidationError("An exact typed recovery authorization is required")
        if (
            type(self.task_version) is not int
            or self.task_version < 1
            or type(self.decision_revision) is not int
            or self.decision_revision < 0
            or type(self.expires_at) not in {int, float}
            or not math.isfinite(self.expires_at)
        ):
            raise ValidationError("Recovery requires explicit versions and a finite expiry")


def _old_plan(row):
    value = row.get("plan")
    if type(value) is not dict or type(row.get("task_version")) is not int:
        raise EvidenceRequired("Original plan and task version require explicit reconciliation")
    value = dict(value)
    value["allowed_files"] = tuple(value["allowed_files"])
    value["artifacts"] = tuple(value["artifacts"])
    if "commit_checks" in value:
        value["commit_checks"] = tuple(value["commit_checks"])
    plan = WorkflowPlan(**value)
    if plan.id != row["plan_id"] or plan.digest != row["plan_digest"]:
        raise AccessDenied("Original workflow plan history changed")
    return plan


def recover_expired(driver, authorization):
    """Called only by trusted operator code; absent from public operation schemas.

    The Core transaction is read-only. Its lock protects eligibility while the
    independent workflow transaction publishes the successor and lineage. No
    provider, resource or worker action is performed here.
    """
    if type(authorization) is not RecoveryAuthorization:
        raise AccessDenied("Trusted typed recovery authorization is required")
    a, scope, context = authorization, driver.scope, driver.core.context
    # Do not let a generic SQLite connector recreate missing Core history.
    Path(context.db.path).stat()
    if a.scope != scope or driver.core.session_binding(driver.handle) != (driver.session_id, scope):
        raise AccessDenied("Recovery authorization belongs to another session")
    request = asdict(a)
    # Exact committed retries return history, including after expiry or progress.
    # They never run the returned successor or refresh its deadline.
    prior = driver.store.recovery_result(scope, a.predecessor, request)
    if prior is not None:
        return prior
    if driver.recovery_history is None:
        raise EvidenceRequired("Original runtime and owner history is unavailable")
    row = driver.store.read(a.predecessor, scope)
    if row["task_id"] != a.task_id:
        raise AccessDenied("Recovery authorization belongs to another task")
    old_plan = _old_plan(row)
    plan = driver.plans.get(a.plan_ref)
    if (
        plan.digest != a.plan_digest
        or plan.id == old_plan.id
        or old_plan.deadline > driver.clock()
        or plan.deadline <= driver.clock()
        or a.expires_at <= driver.clock()
        or plan.deadline > a.expires_at
    ):
        raise AccessDenied("Recovery requires an expired original and a fresh bounded plan")
    history = row.get("recovery_history")
    driver.recovery_history.verify_owner(history, scope)
    # Capture the current owner independently; the stopped descriptor is retained.
    successor_history = driver.recovery_history.capture(scope)
    if history["owner"] == successor_history["owner"]:
        raise AccessDenied("Recovery must retain a distinct previous owner")
    if history["files"]["core"]["path"] != str(Path(context.db.path).absolute()) or history[
        "files"
    ]["workflow"]["path"] != str(driver.store.path):
        raise AccessDenied("Recovery history is not bound to this Core and workflow store")
    task = driver.core.get_task(driver.handle, a.task_id)
    decision = driver._decision(task, "In Progress", "start")
    binding = driver.runtime.binding(task, plan) if driver.runtime else None
    if binding is None:
        raise EvidenceRequired("Fresh registered execution is required")
    with context.db.transaction() as conn:
        if sessions.resolve(conn, driver.handle) != (driver.session_id, scope):
            raise AccessDenied("Recovery session changed")
        task = work.get(conn, driver.session_id, a.task_id)
        context.check(task, a.task_version, a.decision_revision)
        if (
            task.version != row["task_version"]
            or task.decision_revision != row["decision_revision"]
            or task.prose_digest != row["prose_digest"]
            or row["attempt_id"] is not None
            or task.code_bearing
            or task.human_hold
            or task.delivered_once
            or task.completed_work
            or not task.queued_once
            or not task.issue_id
            or task.execution_state != "idle"
            or task.observed_status != IssueStatus.TODO
            or task.desired_status != IssueStatus.TODO
            or task.sync_state != "confirmed"
        ):
            raise EvidenceRequired("Recovery requires the unchanged eligible confirmed task")
        issue_claims.require_open(conn, scope, task.issue_id)
        if conn.execute("SELECT 1 FROM attempts WHERE task_id=?", (task.id,)).fetchone():
            raise EvidenceRequired("Any prior attempt prevents zero-effect recovery")
        if conn.execute(
            "SELECT 1 FROM resource_leases r JOIN attempts a ON a.id=r.attempt_id "
            "WHERE a.session_id=? LIMIT 1",
            (driver.session_id,),
        ).fetchone():
            raise EvidenceRequired("Retained session resources prevent workflow recovery")
        if conn.execute(
            "SELECT 1 FROM outbox WHERE session_id=? AND state NOT IN ('confirmed','failed') "
            "LIMIT 1",
            (driver.session_id,),
        ).fetchone():
            raise EvidenceRequired("Unsettled Core writes prevent workflow recovery")
        with driver.recovery_history.boundary(history, scope):

            def check(original):
                if driver.faults:
                    driver.faults.hit("before:recovery")
                if original != {k: v for k, v in row.items() if k not in {"id", "owner", "steps"}}:
                    raise AccessDenied("Original workflow changed before recovery publication")
                proof = context.decision(conn, task, scope, decision, "In Progress")
                if (
                    proof.expires_at is None
                    or not math.isfinite(proof.expires_at)
                    or plan.deadline > proof.expires_at
                    or a.expires_at <= driver.clock()
                    or plan.deadline <= driver.clock()
                ):
                    raise AccessDenied("Fresh plan exceeds current delegated authority")
                driver.recovery_history.check_bindings(history, successor_history)
                if context.db.guard:
                    context.db.guard(conn)

            reference = driver.store.recover(
                scope,
                a.predecessor,
                task,
                plan,
                a.operation_key,
                binding,
                successor_history,
                request,
                check,
            )
    if driver.faults:
        driver.faults.hit("after:recovery")
    return reference
