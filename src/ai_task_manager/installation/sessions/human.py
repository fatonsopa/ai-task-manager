"""Explicit installer-owned human capture through the existing independent console."""

import math
from dataclasses import asdict

from ai_task_manager.adapters.decisions.human import PREFIX, verify_receipt
from ai_task_manager.adapters.decisions.observations import bind_record, identity
from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.application.service.projection import require_owned
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion
from ai_task_manager.domains.work.models import IssueStatus


def capture_instruction(
    config_file,
    journal,
    *,
    task_id,
    target,
    expected_version,
    decision_revision,
    reason,
    writing_ref=None,
    timeout=60,
    decision_request_id=None,
    decision_option_id=None,
):
    """Not a model tool: the existing private console must approve its own challenge.

    The caller supplies an existing installer-owned HostJournal, never a receipt
    or an observation verdict. This function creates no host store or worker.
    """
    if (
        type(journal) is not HostJournal
        or type(expected_version) is not int
        or type(decision_revision) is not int
        or not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > 4096
        or type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or not 0 < timeout <= 60
    ):
        raise AccessDenied("Exact current work and independent operator capture required")
    target = IssueStatus(target)
    source = {"journal": identity(journal.path), "key": identity(journal.key_path, content=True)}
    HostJournal.open_existing(journal.path, journal.key_path)
    service = load_service(config_file)
    observations = service.decisions.observations
    grant = observations.verify_enrollment(service.grant_ref)
    task = service.core.get_task(service.handle, task_id)
    require_owned(service, task)
    service.core.context.check(task, expected_version, decision_revision)
    proposed = None
    if writing_ref is not None:
        view = service._required("writer").approvals_for_tasks(service.scope)
        proposed = view.verify_approval(writing_ref).digest
    decision_digest = None
    if decision_request_id is not None or decision_option_id is not None:
        import json

        from ai_task_manager.adapters.linear.progress_store import ProgressStore

        if target != task.observed_status or writing_ref is not None:
            raise AccessDenied("An option choice cannot change task status or approved prose")
        row = ProgressStore(service.inbound.journal).get(service.scope, decision_request_id)
        payload = json.loads(row["payload"])
        record = (
            service._required("writer")
            .approvals_for_decisions(service.scope, task.id, task.prose_digest)
            .record(payload["approval_ref"])
        )
        metadata = payload.get("decision")
        if (
            row["task_id"] != task.id
            or row["decision_state"] != "open"
            or row["state"] != "confirmed"
            or row["label_state"] != "confirmed"
            or not metadata
            or metadata != record.get("decision")
            or metadata["recipient"]["id"] != service.scope.principal
            or decision_option_id not in {option["id"] for option in metadata["options"]}
        ):
            raise AccessDenied(
                "Current confirmed request and one exact responsible-human option required"
            )
        decision_digest = row["fingerprint"]
    reference = OperatorChannel(journal).capture_instruction(
        scope=service.scope,
        task_id=task.id,
        target=target,
        revision=task.decision_revision,
        reason=reason,
        task_version=task.version,
        prose_digest=task.prose_digest,
        proposed_prose_digest=proposed,
        timeout=timeout,
        decision_request_id=decision_request_id,
        decision_option_id=decision_option_id,
        decision_digest=decision_digest,
    )
    current = service.core.get_task(service.handle, task.id)
    if current != task:
        raise StaleVersion("Work changed during independent operator capture")
    if observations.verify_enrollment(service.grant_ref) != grant:
        raise AccessDenied("Provider enrollment changed during operator capture")
    if service.core.get_task(service.handle, task.id) != task:
        raise StaleVersion("Work changed during provider verification")
    observations.client.check_connection(service.scope, refresh=True)
    service.seal.check()
    service.claim_store.check()
    if source != {
        "journal": identity(journal.path),
        "key": identity(journal.key_path, content=True),
    }:
        raise AccessDenied("Original operator journal changed")
    record = journal._read(reference, "human")
    receipt = PREFIX + digest(
        {"enrollment": service.grant_ref, "journal": source, "human": reference}
    )
    bind_record(
        service.decisions.journal,
        receipt,
        {
            "enrollment": service.grant_ref,
            "scope": asdict(service.scope),
            "session_id": service.session_id,
            "config": identity(service.seal.path, content=True),
            "capability": identity(read_private(service.seal.path)["session_file"], content=True),
            **source,
            "human_ref": reference,
            "human_hash": digest(record),
            "expires_at": grant.expires_at,
        },
    )
    verify_receipt(service.decisions.journal, receipt, fresh=True)
    return receipt
