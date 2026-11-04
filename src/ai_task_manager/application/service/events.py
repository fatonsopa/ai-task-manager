"""Focused events operations over the configured session."""

import time

from .models import external_result, result
from .projection import event_view


def session_show(service, data, invocation):
    return result(
        {
            "session_id": service.session_id,
            "scope": service.scope,
            "components_configured": {
                name: getattr(service, name) is not None
                for name in ("writer", "provider", "workers", "publisher", "sessions")
            },
            "host_readiness": "not_attested_by_configuration",
        }
    )


def session_resume(service, data, invocation):
    return result(service._required("sessions").resume(data["enrollment_ref"], data["resume_ref"]))


def event_list(service, data, invocation):
    deadline = time.monotonic() + data.get("timeout_seconds", 0)
    while True:
        if service.seal:
            service.seal.check()
        events = service.core.notifications.list_events(service.handle, data.get("after", 0))
        if events or time.monotonic() >= deadline:
            return result([event_view(service, event) for event in events])
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def event_acknowledge(service, data, invocation):
    proof = invocation.reference(
        "notification",
        lambda: service._required("decisions").notification(
            service.scope,
            data["event_id"],
            service.session_id,
            data["stage"],
            data["observation_ref"],
        ),
    )
    committed = service.core.notifications.recover_acknowledge(
        service.handle, data["event_id"], data["stage"], proof, data["operation_key"]
    )
    if committed is not None:
        return result({"stage": committed})
    service._required("decisions").validate_notification_source(
        service.scope,
        data["event_id"],
        service.session_id,
        data["stage"],
        data["observation_ref"],
        proof,
    )
    stage = service.core.notifications.acknowledge(
        service.handle, data["event_id"], data["stage"], proof, data["operation_key"]
    )
    return result({"stage": stage})


def sync_now(service, data, invocation):
    provider = service._required("provider")
    outbound = provider.run_once(service.handle)
    inbound = service.inbound.run_once(service.handle) if service.inbound else None
    operations = service.core.sync.list_operations(service.handle)
    unconfirmed = sum(
        task.sync_state != "confirmed" for task in service.core.list_tasks(service.handle)
    )
    unresolved = [item.state for item in operations if item.state != "confirmed"]
    observed = [item.get("state") for item in (outbound, inbound) if isinstance(item, dict)]
    state = (
        "uncertain"
        if "uncertain" in unresolved + observed
        else "failed"
        if any(x in {"failed", "conflict"} for x in observed)
        else "pending"
        if unresolved or unconfirmed
        else "confirmed"
    )
    return external_result(
        {
            "state": state,
            "outbound": outbound,
            "inbound": inbound,
            "unconfirmed_tasks": unconfirmed,
            "unresolved_operations": len(unresolved),
        }
    )
