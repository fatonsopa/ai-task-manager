"""Bind native notification receipts to an existing provider enrollment."""

from dataclasses import asdict, replace

from ai_task_manager.adapters.hosts.bridge.enrollment import HostObservations
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.model_guard import verify_model_history
from ai_task_manager.adapters.hosts.bridge.observations import (
    NotificationObserver,
    NotificationReceipts,
)
from ai_task_manager.adapters.hosts.bridge.startup import HostStartupReceipts, StartupObserver
from ai_task_manager.adapters.hosts.bridge.transcript import OwnedTranscript
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired

from .observations import bind_record, current_enrollment, identity

SOURCE = "session-host-source:"
RECEIPT = "session-host-receipt:"


def bind(owner, grant, observer, session_id, lead_enrollment_ref):
    """Called only by the trusted native gateway with its actual producer objects."""
    if (
        type(observer) is not NotificationObserver
        or type(observer.transcript) is not OwnedTranscript
        or type(observer.startup) is not StartupObserver
        or type(observer.journal) is not HostJournal
        or observer.startup.transcript is not observer.transcript
        or observer.journal is not observer.transcript.journal
        or observer.binding is not observer.transcript.binding
        or observer.binding.scope != grant.scope
        or observer.binding.core_session != session_id
    ):
        raise AccessDenied("Exact native notification producer and enrolled session required")
    observer.startup.policy.check()
    observer.startup.assert_stable()
    startup = HostStartupReceipts(
        observer.journal, {observer.binding.attempt_id: observer.transcript.process}
    ).verify_host_ready(observer.startup.ready_ref)
    if startup.scope != grant.scope or startup.session_id != observer.startup.native_session:
        raise AccessDenied("Native startup differs from the provider enrollment")
    record = observer.journal._read(observer.startup.ready_ref, "startup")
    enrollment = observer.journal._read(lead_enrollment_ref)
    delegated = enrollment["kind"] == "delegated_enrollment"
    if delegated:
        from ai_task_manager.installation.native.delegated_lead import verify

        lead = verify(observer.journal, enrollment, grant, asdict(observer.binding), session_id)
    else:
        lead = HostObservations(observer.journal).verify_enrollment(lead_enrollment_ref)
    payload = enrollment["payload"]
    if (
        lead.scope != grant.scope
        or payload["startup_ref"] != record["ref"]
        or not delegated
        and (
            type(payload.get("operator_pid")) is not int
            or payload["operator_pid"] <= 0
            or not isinstance(payload.get("source_hash"), str)
            or len(payload["source_hash"]) != 64
        )
        or not set(lead.actions) <= set(grant.actions)
    ):
        raise AccessDenied("Independent lead enrollment differs from the provider/native scope")
    value = {
        "enrollment": grant.source_ref,
        "scope": asdict(grant.scope),
        "session_id": session_id,
        "binding": asdict(observer.binding),
        "process": vars(observer.transcript.process.verify_identity()),
        "startup_ref": record["ref"],
        "startup_hash": digest(record),
        "lead_enrollment_ref": lead_enrollment_ref,
        "lead_enrollment_hash": digest(enrollment),
        "native_session": observer.startup.native_session,
        "journal": identity(observer.journal.path),
        "key": identity(observer.journal.key_path, content=True),
        "expires_at": min(grant.expires_at, observer.binding.deadline, lead.expires_at),
    }
    if delegated:
        value["delegated_event_id"] = payload["event_id"]
    reference = SOURCE + digest(grant.source_ref)
    bind_record(owner.journal, reference, value)
    return reference


def _journal(authority, source):
    provider = current_enrollment(authority, source["enrollment"], source["scope"])
    for name in ("journal", "key"):
        if identity(source[name]["path"], content=name == "key") != source[name]:
            raise AccessDenied("Enrolled native notification storage changed")
    journal = HostJournal.open_existing(
        source["journal"]["path"], source["key"]["path"], clock=authority.clock
    )
    # Startup is historical provenance after its admission window, not renewed authority.
    startup = journal._read(source["startup_ref"], "startup", fresh=False)
    if digest(startup) != source["startup_hash"]:
        raise AccessDenied("Original native startup changed")
    enrollment = journal._read(source["lead_enrollment_ref"])
    if digest(enrollment) != source["lead_enrollment_hash"]:
        raise AccessDenied("Original lead enrollment changed or expired")
    if "delegated_event_id" in source:
        from ai_task_manager.adapters.decisions.authority import HostGrant
        from ai_task_manager.domains.work.models import Scope
        from ai_task_manager.installation.native.delegated_lead import verify

        grant = HostGrant(
            Scope(**provider["scope"]),
            provider["scope"]["logical_session"],
            tuple(provider["actions"]),
            source["enrollment"],
            provider["expires_at"],
        )
        verify(journal, enrollment, grant, source["binding"], source["session_id"])
        if enrollment["payload"]["event_id"] != source["delegated_event_id"]:
            raise AccessDenied("Delegated event differs from original notification source")
    elif enrollment["kind"] != "enrollment":
        raise AccessDenied("Human enrollment provenance changed")
    verify_model_history(journal, startup["payload"], source["process"])
    if source["binding"]["host"] == "codex":
        for row in journal._history("trace"):
            trace = row["payload"]
            if not all(
                trace.get(key) == value
                for key, value in {
                    "process": source["process"],
                    "attempt_id": source["binding"]["attempt_id"],
                    "worker_id": source["binding"]["worker_id"],
                    "configuration_digest": source["binding"]["configuration_digest"],
                }.items()
            ):
                continue
            frame = trace.get("frame", {})
            if frame.get("method") == "account/updated" and (
                frame.get("params", {}).get("authMode") != "chatgpt"
            ):
                raise AccessDenied("Owned native authentication changed after enrollment")
    return journal


def _observed(authority, source, reference):
    journal = _journal(authority, source)
    row = journal._read(reference, "notification")
    data = row["payload"]
    if "delegated_event_id" in source and data.get("event_id") != source["delegated_event_id"]:
        raise AccessDenied("Notification is outside the explicitly delegated event")
    if any(
        data.get(key) != value
        for key, value in {
            "scope": source["scope"],
            "session_id": source["session_id"],
            "native_session": source["native_session"],
            "attempt_id": source["binding"]["attempt_id"],
        }.items()
    ):
        raise AccessDenied("Native notification belongs to another producer or session")
    sources = [journal._read(ref) for ref in row["sources"]]
    if not sources or digest(sources) != data["trace_hash"]:
        raise AccessDenied("Native notification source provenance changed")
    prior = {"delivered": None, "ai_acknowledged": "delivered", "user_reported": "ai_acknowledged"}
    if data["stage"] not in prior:
        raise AccessDenied("Unknown native notification stage")
    parents = [s for s in sources if s["kind"] == "notification"]
    expected = prior[data["stage"]]
    if len(parents) != int(expected is not None):
        raise AccessDenied("Native notification stage provenance is incomplete")
    if parents:
        parent = _observed(authority, source, parents[0]["ref"])
        if (parent.event_id, parent.stage) != (data["event_id"], expected):
            raise AccessDenied("Native notification stage belongs to another event")
    for item in sources:
        if item["kind"] != "trace":
            continue
        trace = item["payload"]
        if "frame" in trace and any(
            trace.get(k) != v
            for k, v in {
                "process": source["process"],
                "attempt_id": source["binding"]["attempt_id"],
                "worker_id": source["binding"]["worker_id"],
                "configuration_digest": source["binding"]["configuration_digest"],
            }.items()
        ):
            raise AccessDenied("Native notification trace owner changed")
    result = NotificationReceipts(journal).verify_notification(reference)
    for name in ("journal", "key"):
        if identity(source[name]["path"], content=name == "key") != source[name]:
            raise AccessDenied("Native notification storage changed during observation")
    return result


def observe(owner, grant, reference):
    source_ref = SOURCE + digest(grant.source_ref)
    try:
        source = owner.journal._get(source_ref, SOURCE)
    except AccessDenied as error:
        raise EvidenceRequired(
            "Bound native notification source is missing, changed or expired"
        ) from error
    if source["scope"] != asdict(grant.scope):
        raise AccessDenied("Native source belongs to another provider enrollment")
    result = _observed(owner.journal, source, reference)
    ref = RECEIPT + digest({"source": source_ref, "observation": reference})
    bind_record(
        owner.journal,
        ref,
        {
            "source": source_ref,
            "observation": reference,
            "observed": asdict(result),
            "expires_at": min(grant.expires_at, source["expires_at"]),
        },
    )
    return replace(result, source_ref=ref, expires_at=min(grant.expires_at, source["expires_at"]))


def verify_receipt(authority, reference):
    """Local-only consumption check, including after an interrupted proof issuance."""
    receipt = authority._get(reference, RECEIPT)
    source = authority._get(receipt["source"], SOURCE)
    observed = _observed(authority, source, receipt["observation"])
    if asdict(observed) != receipt["observed"]:
        raise AccessDenied("Original native notification observation changed")
    return observed
