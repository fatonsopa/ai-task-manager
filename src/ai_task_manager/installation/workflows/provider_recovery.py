"""Reconcile one retained workflow's provider identities without renewing execution."""

import time
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.progress_store import ProgressStore
from ai_task_manager.application.linear_sync.progress import (
    ProgressApprovalContext,
    ProgressPublisher,
)
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.application.service.models import result
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied, WorkError
from ai_task_manager.installation.sessions.recovery import history, installed_authority


def reconcile_original(recovery, service, row):
    """Only trusted finite recovery authority may consume original retained evidence."""
    if service.provider is None or not recovery.config.get("linear"):
        return None
    task_id = row["task_id"]
    operations = [
        operation
        for operation in service.core.sync.list_operations(service.handle)
        if operation.task_id == task_id
        and not operation.fenced_by
        and operation.state != "confirmed"
    ]
    journal, client = service.provider.journal, service.provider.client
    publications = ProgressStore.retained(journal, service.scope, task_id)
    deliveries = service.inbound.next_receipt(service.handle, task_ids=(task_id,))
    if not operations and not publications and deliveries is None:
        return None
    if len(operations) > 32 or len(publications) > 32:
        raise AccessDenied("Retained provider recovery exceeds its bounded task slice")

    def check():
        recovery.check_service(service)
        from ai_task_manager.adapters.decisions.observations import intent

        installed_authority(recovery.config)
        enrollment = recovery.data.get("enrollment")
        reference = recovery.config.get("grant_ref")
        if (
            enrollment is None
            or history(recovery.journal, reference) != enrollment
            or enrollment["scope"] != recovery.spec.data["scope"]
            or enrollment["config_path"] != str(recovery.config_file)
            or enrollment["config_intent"] != intent(recovery.config)
            or enrollment["provider"] != asdict(client.binding.expected)
            or history(recovery.journal, "session-revoked:" + digest(reference))
            != recovery.data.get("enrollment_revocation")
        ):
            raise AccessDenied("Original provider enrollment changed during recovery")
        # The original expiry/revocation remains history. This current capability
        # permits reads and local settlement, never a new provider mutation.
        service.seal.check()
        client.check_connection(service.scope)

    def execution():
        from ai_task_manager.application.workflows.inspection import current_grant

        check()
        if time.time() >= recovery.spec.data["deadline"]:
            raise AccessDenied("Original execution deadline expired")
        current_grant(service)

    check()
    observed = []
    reconcile = Reconciliation(service.core, client, journal)
    for operation in operations:
        check()
        try:
            sends = [s for s in journal.sends(service.scope) if s["operation_id"] == operation.id]
            if sends:
                reconcile.recover_outbound(service.handle, operation.id, check_authority=check)
                observed.append({"operation_id": operation.id, "state": "confirmed"})
            elif operation.state in {"pending", "failed", "uncertain"}:
                # Observation authority cannot turn absence into execution permission.
                execution()
                if operation.state != "pending":
                    reconcile.retry_unsent(service.handle, operation.id, check_authority=execution)
                outcome = service.provider.run_once(
                    service.handle, task_id=task_id, check_authority=execution
                )
                observed.append({"operation_id": operation.id, "state": outcome["state"]})
            else:
                observed.append({"operation_id": operation.id, "state": "unsent"})
        except (LinearError, WorkError) as error:
            check()
            state = "uncertain"
            if not any(s["operation_id"] == operation.id for s in journal.sends(service.scope)):
                journal.unsent_receipt(service.scope, operation)
                check()
                state = "unsent"
            observed.append(
                {
                    "operation_id": operation.id,
                    "state": state,
                    "reason": error.category
                    if isinstance(error, LinearError)
                    else type(error).__name__,
                }
            )

    for publication in publications:
        check()
        task = service.core.get_task(service.handle, task_id)
        writer = service._required("writer")
        approvals = (
            writer.approvals_for_decisions
            if publication["decision_state"] is not None
            else writer.approvals_for_progress
        )(service.scope, task.id, task.prose_digest)
        publisher = ProgressPublisher(
            service.core,
            client,
            journal,
            approval_policy=service.core.context.approval_policy,
            approvals=approvals,
            approval_context=ProgressApprovalContext(service.scope, task.id, task.prose_digest),
            check_authority=check,
            existing_only=True,
        )
        try:
            value = publisher.recover(
                service.handle, publication["id"], observations=service.decisions.observations
            )
            observed.append({"publication_id": publication["id"], "state": value["state"]})
        except (LinearError, WorkError) as error:
            observed.append(
                {
                    "publication_id": publication["id"],
                    "state": "uncertain",
                    "reason": error.category
                    if isinstance(error, LinearError)
                    else type(error).__name__,
                }
            )

    for _ in range(8):
        check()
        value = service.inbound.run_once(service.handle, check_authority=check, task_ids=(task_id,))
        if value is None:
            break
        observed.append({"inbound": value})
        if value.get("state") in {"pending", "conflict"}:
            break

    check()
    task = service.core.get_task(service.handle, task_id)
    pending = row["steps"].get("confirm")
    if (
        pending
        and pending["result"] is None
        and task.sync_state == "confirmed"
        and task.observed_status == pending["payload"]["desired"]
        and task.prose_digest == row["prose_digest"]
        and task.decision_revision == row["decision_revision"]
        and not task.human_hold
        and any(
            value.get("operation_id") and value.get("state") == "confirmed" for value in observed
        )
    ):
        store = recovery.store()
        recovery.release_claim(store, service.scope)
        owner = "provider-recovery:" + str(uuid4())
        store.claim(row["id"], service.scope, owner)
        try:
            check()
            store.finish(
                row["id"],
                owner,
                "confirm",
                {
                    "observed": task.observed_status,
                    "reconciled": True,
                    "recovery_authority": recovery.reference,
                },
            )
        finally:
            store.release(row["id"], owner)
    uncertain = any(value.get("state") == "uncertain" for value in observed)
    pending = any(value.get("state") in {"pending", "unsent"} for value in observed)
    try:
        execution()
    except WorkError:
        answer = result(
            {
                "state": "uncertain" if uncertain else "checkpoint",
                "reason": "provider_reconciliation_pending"
                if uncertain or pending
                else "original_execution_unavailable",
                "workflow": row["id"],
                "provider_recovery": observed,
            },
            "uncertain" if uncertain else "blocked",
        )
        answer["exit_code"] = 6 if uncertain else 4
        return answer
    if uncertain or pending:
        answer = result(
            {
                "state": "uncertain" if uncertain else "checkpoint",
                "reason": "provider_reconciliation_pending",
                "workflow": row["id"],
                "provider_recovery": observed,
            },
            "uncertain" if uncertain else "blocked",
        )
        answer["exit_code"] = 6 if uncertain else 4
        return answer
    return None
