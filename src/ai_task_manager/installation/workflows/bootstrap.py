"""Explicit trusted preparation before a task-bound execution owner is admitted."""

import os
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
    setup_lock,
)

OPERATIONS = frozenset(
    {
        "writing_prepare",
        "writing_review",
        "writing_finalize",
        "task_submit",
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
        grant = service.decisions.observations.verify_enrollment(service.grant_ref)
        if (
            type(grant) is not HostGrant
            or grant.scope != service.scope
            or grant.logical_session != service.scope.logical_session
            or grant.expires_at <= service.core.context.clock()
        ):
            raise AccessDenied("Current same-session operator enrollment required")
        with service.core.context.db.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM attempts WHERE session_id=? LIMIT 1", (service.session_id,)
            ).fetchone():
                raise EvidenceRequired("Execution history forbids initial preparation")
            if conn.execute(
                "SELECT 1 FROM outbox WHERE session_id=? "
                "AND state IN ('pending','leased','uncertain') LIMIT 1",
                (service.session_id,),
            ).fetchone():
                raise EvidenceRequired(
                    "Unsettled effects require reconciliation before preparation"
                )
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


def activation_reference(config_file, control):
    return "workflow-owner-activation:" + digest(
        {"config": str(Path(config_file).absolute()), "instance": control["instance"]}
    )
