"""Explicit trusted preparation before a task-bound execution owner is admitted."""

import json
import os
from contextlib import contextmanager
from pathlib import Path

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.workflow_control.identity import file_binding, settings
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.sessions.recovery import (
    history,
    installed_authority,
    installed_provider_history,
    intake_recovery_authorization,
    setup_lock,
)

OPERATIONS = frozenset(
    {
        "writing_prepare",
        "writing_review",
        "writing_finalize",
        "task_submit",
        "task_find",
        "task_inspect",
        "task_select",
        "task_admit",
        "task_show",
        "task_list",
        "task_action",
    }
)


def prepare(config_file, request):
    """Operator CLI port; never an ordinary forwarding fallback or model tool."""
    if (
        type(request) is not dict
        or set(request) != {"operation", "data"}
        or request["operation"] not in OPERATIONS
        or type(request["data"]) is not dict
    ):
        raise AccessDenied("Explicit initial preparation operation required")
    operation, data = request["operation"], request["data"]
    if operation == "task_action" and data.get("action") != "enqueue":
        raise AccessDenied("Initial preparation permits only approved enqueue")
    if operation == "task_submit" and ({"parent_task_id", "followup_ref"} & data.keys()):
        raise AccessDenied("Initial preparation cannot admit delivered follow-up work")
    with _initial_service(config_file) as (service, control, grant):
        service.core.intake.require_settled_preparation(service.handle)
        reference = "workflow-bootstrap:" + digest(
            {"instance": control["instance"], "request": request}
        )
        bind_record(
            service.decisions.journal,
            reference,
            {
                "config": file_binding(config_file),
                "session": service.session_id,
                "owner_instance": control["instance"],
                "operation": operation,
                "request_digest": digest(request),
                "expires_at": grant.expires_at,
            },
        )
        result = service.call(operation, data)
        service.decisions.observations.verify_enrollment(service.grant_ref)
        service.seal.check()
        service.claim_store.check()
        return result


@contextmanager
def _initial_service(config_file, *, recovery=None):
    with setup_lock(config_file):
        config = read_private(config_file)
        from ai_task_manager.config.capacity import policies

        if not policies(config):
            raise EvidenceRequired("Initial owner preparation needs declared finite capacity")
        control = settings(config.get("workflow_control"))
        for key in ("record", "endpoint"):
            path = Path(control[key])
            if (
                path.parent.resolve() != path.parent
                or not path.parent.is_dir()
                or path.parent.stat().st_uid != os.getuid()
                or path.parent.stat().st_mode & 0o077
            ):
                raise AccessDenied("Initial owner directory must be private and physical")
            if path.exists() or path.is_symlink():
                raise AccessDenied("Existing owner state forbids initial preparation")
        installed_authority(config)
        installed_provider_history(config)
        # A fixed production composition, reached only through this explicit port.
        # Ordinary load_service and every ordinary CLI/MCP call retain forwarding.
        service = load_service(config_file, workflow_factory=lambda *_: None)
        if (
            history(service.decisions.journal, activation_reference(config_file, control))
            is not None
        ):
            raise AccessDenied("Owner activation history permanently closes initial preparation")
        if recovery is None:
            grant = service.decisions.observations.verify_enrollment(service.grant_ref)
            if (
                type(grant) is not HostGrant
                or grant.scope != service.scope
                or grant.logical_session != service.scope.logical_session
                or grant.expires_at <= service.core.context.clock()
            ):
                raise AccessDenied("Current same-session operator enrollment required")
        else:
            grant = intake_recovery_authorization(service, config_file, config, **recovery)
        with service.core.context.db.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM attempts WHERE session_id=? LIMIT 1", (service.session_id,)
            ).fetchone():
                raise EvidenceRequired("Execution history forbids initial preparation")
        yield service, control, grant


def repair_pending_intake(config_file, task_id, create_operation_id, writing_ref, operation_key):
    """Trusted same-issue repair; no public request can select this operation."""
    from ai_task_manager.application.linear_sync.reconcile import Reconciliation

    with _initial_service(config_file) as (service, _, _):

        def current_authority():
            service.decisions.observations.verify_enrollment(service.grant_ref)
            service.seal.check()
            service.claim_store.check()

        prose = service.writer.approvals_for_tasks(service.scope).prose(writing_ref)
        provider = service.provider
        task = Reconciliation(
            service.core, provider.client, provider.journal
        ).repair_pending_create(
            service.handle,
            task_id,
            create_operation_id,
            prose,
            writing_ref,
            operation_key,
            check_authority=current_authority,
        )
        current_authority()
        return service.confirm(task)


def recover_pending_intake(
    config_file, task_id, create_operation_id, writing_ref, operation_key, *, recovery_ref, deadline
):
    """Explicit operator-authorized readback only; never drain an unsent effect."""
    from ai_task_manager.application.linear_sync.inflight import request_in_flight
    from ai_task_manager.application.linear_sync.reconcile import Reconciliation
    from ai_task_manager.application.service.models import result
    from ai_task_manager.application.service.projection import task_view

    target = dict(
        task_id=task_id,
        create_operation_id=create_operation_id,
        writing_ref=writing_ref,
        operation_key=operation_key,
    )
    recovery = dict(target=target, recovery_ref=recovery_ref, deadline=deadline)
    with _initial_service(config_file, recovery=recovery) as (service, _, check):
        prose = service.writer.approvals_for_tasks(service.scope).prose(writing_ref)
        task = service.core.intake.pending_create_repair_replay(
            service.handle, task_id, create_operation_id, prose, writing_ref, operation_key
        )
        if task is None or task.human_hold or task.queued_once or task.delivered_once:
            raise EvidenceRequired("Read-only recovery requires the exact existing initial repair")
        operations = service.core.sync.list_operations(service.handle)
        original = next((op for op in operations if op.id == create_operation_id), None)
        updates = [
            op
            for op in operations
            if op.kind == "update_prose"
            and op.task_id == task.id
            and original
            and op.payload.get("event_id") == original.fenced_by
        ]
        if len(updates) != 1 or updates[0].state not in {"uncertain", "failed", "confirmed"}:
            raise EvidenceRequired("Read-only recovery requires an already-sent linked update")
        update = updates[0]
        if update.fenced_by or any(
            op.state in {"pending", "leased", "uncertain"} and op.id not in {original.id, update.id}
            for op in operations
        ):
            raise EvidenceRequired("Unrelated unsettled effects forbid intake recovery")
        provider = service.provider
        connection = provider.client.check_connection(service.scope, refresh=True)
        provider.journal.pending_create_repair_snapshot(
            service.scope, task, update, original, connection
        )
        sends = provider.journal.sends(service.scope)
        sent = [row for row in sends if row["operation_id"] == update.id]
        if (
            len(sent) != 1
            or not sent[0]["snapshot"]
            or sent[0]["attempt"] != update.attempt_count
            or sent[0]["connection_digest"] != connection.digest
            or any(
                row["state"] not in {"confirmed", "failed"}
                and row["operation_id"] not in {original.id, update.id}
                for row in sends
            )
            or request_in_flight(provider.journal, service.scope, update)
            or request_in_flight(provider.journal, service.scope, original)
        ):
            raise EvidenceRequired("Read-only recovery requires a known quiescent update result")
        if json.loads(sent[0]["snapshot"])["id"] != task.issue_id:
            raise EvidenceRequired("Repair update result has another identity")
        check()
        task = Reconciliation(
            service.core, provider.client, provider.journal
        ).repair_pending_create(
            service.handle,
            task_id,
            create_operation_id,
            prose,
            writing_ref,
            operation_key,
            check_authority=check,
            read_only=True,
        )
        check()
        if task.sync_state != "confirmed":
            raise EvidenceRequired("Read-only repair remains unconfirmed")
        # A previous finite call may have confirmed Core just before its authority
        # expired. Finish only that known journal effect after another exact read.
        current_send = next(
            row for row in provider.journal.sends(service.scope) if row["id"] == sent[0]["id"]
        )
        if current_send["state"] != "confirmed":
            snapshot = provider.client.require_delegate(provider.client.read_issue(task.issue_id))
            if (
                snapshot.title != task.title
                or snapshot.body != task.body
                or snapshot.status != task.observed_status
                or snapshot.assignee_id != service.scope.principal
            ):
                raise EvidenceRequired("Confirmed repair readback changed")
            check()
            provider.journal.finish_send(current_send["id"], "confirmed", snapshot)
        check()
        if service.core.get_task(service.handle, task.id).version != task.version:
            raise EvidenceRequired("Repaired task changed during read-only recovery")
        return result(task_view(service, task), "confirmed")


def activation_reference(config_file, control):
    return "workflow-owner-activation:" + digest(
        {"config": str(Path(config_file).absolute()), "instance": control["instance"]}
    )
