"""Discovery is readable; admission consumes independent selection and prose authority."""

from ai_task_manager.adapters.decisions.existing import SelectionAuthority
from ai_task_manager.domains.work.errors import EvidenceRequired

from .models import result
from .projection import task_view


def task_find(service, data, invocation):
    return result(
        service._required("existing").find(data["query"], data.get("limit", 20), data.get("cursor"))
    )


def task_inspect(service, data, invocation):
    return result(service._required("existing").inspect(data["issue"]))


def task_select(service, data, invocation):
    existing = service._required("existing")
    discovery = existing.reader.receipt(data["discovery_ref"])
    service.core.existing.observed(service.handle, data["discovery_ref"])
    observer = service._required("decisions").observations
    capture = getattr(observer, "capture_issue_selection", None)
    if capture is None or service.claim_store is None:
        raise EvidenceRequired("A trusted installed lead-selection observer is required")
    claim_store = service.claim_store.context
    observation_ref = invocation.reference(
        "selection_observation",
        lambda: capture(service.scope, discovery, claim_store, invocation.id, data),
    )
    authority = SelectionAuthority(service.decisions.journal, observer)
    reference = invocation.reference(
        "selection",
        lambda: authority.issue(
            service.scope,
            discovery.subject,
            discovery.payload["discovery_digest"],
            claim_store,
            observation_ref,
        ),
    )
    proof = authority.validate(
        reference,
        service.scope,
        discovery.subject,
        discovery.payload["discovery_digest"],
        claim_store,
        observation_ref,
    )
    return result(
        {"selection_ref": reference, "issue_id": proof.subject, "expires_at": proof.expires_at}
    )


def task_admit(service, data, invocation):
    prior = service.core.existing.recover(
        service.handle,
        data["discovery_ref"],
        data["writing_ref"],
        data["selection_ref"],
        data["operation_key"],
    )
    if prior is not None:
        return result(task_view(service, prior))
    prose = (
        service._required("writer").approvals_for_tasks(service.scope).prose(data["writing_ref"])
    )
    task = service._required("existing").admit(
        data["discovery_ref"],
        prose,
        data["writing_ref"],
        data["selection_ref"],
        data["operation_key"],
        check_selection=lambda: SelectionAuthority(
            service.decisions.journal, service.decisions.observations
        ).current(data["selection_ref"]),
    )
    return result(task_view(service, service.core.get_task(service.handle, task.id)))
