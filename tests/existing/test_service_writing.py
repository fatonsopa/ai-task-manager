import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.domains.work.existing import ExistingBlocked
from ai_task_manager.domains.work.models import TaskProse
from tests.application.helpers import FEATURE_BODY

from .service_support import approve, compose


@pytest.fixture
def service(rig):
    return compose(rig)


def prepare(rig, service, provider, status="Todo"):
    issue = provider.seed(status)
    provider.issues[issue]["description"] = FEATURE_BODY
    approved = approve(service, provider.issues[issue]["title"], FEATURE_BODY)
    inspected = service.call("task_inspect", {"issue": issue})
    assert inspected["ok"], inspected
    ref = inspected["data"]["discovery_ref"]
    return (
        issue,
        approved,
        {
            "discovery_ref": ref,
            "writing_ref": approved["approval_ref"],
            "selection_ref": rig.select(ref),
            "operation_key": str(uuid4()),
        },
    )


def test_ea21_ea55_actual_writer_service_existing_todo_chain(rig, service, provider):
    issue, approval, data = prepare(rig, service, provider)
    handoff = approval["handoff"]
    assert hashlib.sha256(handoff["message"].encode()).hexdigest() == handoff["message_sha256"]
    admitted = service.call("task_admit", data)
    assert admitted["ok"] and admitted["data"]["issue_id"] == issue, admitted
    assert not admitted["data"]["queued_once"]
    task = rig.core.get_task(rig.handle, admitted["data"]["id"])
    queued = service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": "enqueue",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": "queue-current-existing",
        },
    )
    assert queued["ok"] and queued["data"]["queued_once"], queued
    assert queued["data"]["observed_status"] == "Todo"
    assert service.call("task_show", {"task_id": task.id})["data"]["title"] == task.title
    rig.assert_no_provider_mutations()


@pytest.mark.parametrize(
    "missing", ["discovery_ref", "writing_ref", "selection_ref", "operation_key"]
)
def test_ea56_schema_omission_has_no_effect(rig, service, provider, missing):
    data = {
        "discovery_ref": "none",
        "writing_ref": "none",
        "selection_ref": "none",
        "operation_key": "omission",
    }
    del data[missing]
    before, requests = rig.effects(), len(provider.requests)
    result = service.call("task_admit", data)
    assert not result["ok"] and result["outcome"] == "invalid_request"
    assert rig.effects() == before and len(provider.requests) == requests


@pytest.mark.parametrize("authority", ["human", "selected", "never_delivered", "scope", "command"])
def test_ea56_unknown_self_authority_fields_are_rejected(rig, service, provider, authority):
    before = rig.effects()
    data = {
        "discovery_ref": "none",
        "writing_ref": "none",
        "selection_ref": "none",
        "operation_key": "authority",
        authority: True,
    }
    result = service.call("task_admit", data)
    assert not result["ok"] and result["outcome"] == "invalid_request"
    assert rig.effects() == before


def test_ea23_actual_writer_scope_and_progress_purpose_are_not_interchangeable(
    rig, service, provider
):
    issue, approved, data = prepare(rig, service, provider)
    other = replace(rig.scope, logical_session="other")
    foreign = approve(service, provider.issues[issue]["title"], FEATURE_BODY, scope=other)
    before = rig.effects()
    refused = service.call("task_admit", {**data, "writing_ref": foreign["approval_ref"]})
    assert not refused["ok"] and refused["outcome"] == "denied"
    assert rig.effects() == before
    admitted = service.call("task_admit", {**data, "operation_key": "real-admission"})
    assert admitted["ok"], admitted
    task = rig.core.get_task(rig.handle, admitted["data"]["id"])
    progress = approve(
        service,
        "ignored",
        "The fixture check passed; delivery remains pending.",
        purpose="progress",
        task=task,
    )
    refused = service.call(
        "task_admit",
        {**data, "writing_ref": progress["approval_ref"], "operation_key": "progress-not-task"},
    )
    assert not refused["ok"] and refused["outcome"] == "denied"


@pytest.mark.parametrize("field", ["provider", "workspace", "principal", "project"])
def test_ea23_actual_writer_other_scope_fields_cannot_authorize_admission(
    rig, service, provider, field
):
    issue = provider.seed()
    provider.issues[issue]["description"] = FEATURE_BODY
    other_scope = replace(rig.scope, **{field: str(uuid4())})
    approved = approve(service, provider.issues[issue]["title"], FEATURE_BODY, scope=other_scope)
    ref = rig.intake.inspect(issue)["discovery_ref"]
    before = rig.effects()
    refused = service.call(
        "task_admit",
        {
            "discovery_ref": ref,
            "writing_ref": approved["approval_ref"],
            "selection_ref": rig.select(ref),
            "operation_key": str(uuid4()),
        },
    )
    assert not refused["ok"] and refused["outcome"] == "denied"
    assert rig.effects() == before


def test_ea25_new_admission_rejects_current_policy_mismatch(rig, service, provider):
    issue, approved, data = prepare(rig, service, provider)
    rig.core.context.approval_policy = replace(
        rig.core.context.approval_policy, writer_hash="f" * 64
    )
    before = rig.effects()
    refused = service.call("task_admit", data)
    assert not refused["ok"] and refused["outcome"] == "denied", refused
    assert rig.effects() == before


@pytest.mark.parametrize("change", ["title", "body", "metadata"])
def test_ea24_actual_approval_binds_exact_prose_and_semantic_metadata(
    rig, service, provider, change
):
    issue, approved, data = prepare(rig, service, provider)
    ref = data["discovery_ref"]
    prose = TaskProse(provider.issues[issue]["title"], FEATURE_BODY)
    prose = replace(
        prose,
        **{
            "title": {"title": prose.title + " changed"},
            "body": {"body": prose.body + "\n"},
            "metadata": {"required_work": ("different",)},
        }[change],
    )
    before = rig.effects()
    from ai_task_manager.domains.work.errors import ApprovalRequired

    key = str(uuid4())
    fresh = rig.admission_readback(ref, key)
    with pytest.raises(ApprovalRequired):
        rig.core.existing.admit(
            rig.handle,
            ref,
            fresh,
            prose,
            approved["approval_ref"],
            data["selection_ref"],
            key,
        )
    assert rig.effects() == before


def test_ea25_ea49_current_new_effect_refuses_altered_artifact_committed_retry_survives(
    rig, service, provider
):
    issue, approved, data = prepare(rig, service, provider)
    original_invoke = service._invoke

    # Simulate a reply loss after Core commit, before the public invocation result is saved.
    def lost(operation, args, invocation):
        result = original_invoke(operation, args, invocation)
        if operation == "task_admit":
            raise RuntimeError("Injected reply loss after committed admission")
        return result

    service._invoke = lost
    uncertain = service.call("task_admit", data)
    assert not uncertain["ok"]
    assert len(rig.core.list_tasks(rig.handle)) == 1
    service._invoke = original_invoke
    Path(approved["handoff"]["destination"]).write_text("Altered after committed admission\n")
    before = rig.effects()
    replay = service.call("task_admit", data)
    assert replay["ok"] and len(rig.core.list_tasks(rig.handle)) == 1, replay
    refused = service.call("task_admit", {**data, "operation_key": "new-effect-altered"})
    assert not refused["ok"] and refused["outcome"] == "denied", refused
    assert rig.effects() == before
    rig.assert_no_provider_mutations()


def test_ea51_cached_admission_rechecks_current_quarantine(rig, service, provider):
    issue, approved, data = prepare(rig, service, provider)
    result = service.call("task_admit", data)
    task = rig.core.get_task(rig.handle, result["data"]["id"])
    from tests.linear.helpers import uid

    provider.issues[issue]["assignee"] = {"id": uid(999)}
    provider.issues[issue]["description"] = "FOREIGN_NEW_PROSE"
    with pytest.raises(ExistingBlocked):
        rig.intake.guard(task)
    replay = service.call("task_admit", data)
    assert replay["ok"] and replay["data"]["quarantined"]
    assert replay["data"]["body"] is None and replay["data"]["observed_status"] is None


def test_ea57_ea58_actual_order_omissions_do_not_admit_or_dispatch(rig, service, provider):
    issue = provider.seed()
    provider.issues[issue]["description"] = FEATURE_BODY
    approved = approve(service, provider.issues[issue]["title"], FEATURE_BODY)
    before = rig.effects()
    no_inspect = service.call(
        "task_admit",
        {
            "discovery_ref": "never-inspected",
            "writing_ref": approved["approval_ref"],
            "selection_ref": "never-selected",
            "operation_key": "order-inspect",
        },
    )
    assert not no_inspect["ok"] and rig.effects() == before
    inspected = service.call("task_inspect", {"issue": issue})["data"]
    no_selection = service.call(
        "task_admit",
        {
            "discovery_ref": inspected["discovery_ref"],
            "writing_ref": approved["approval_ref"],
            "selection_ref": "never-selected",
            "operation_key": "order-selection",
        },
    )
    assert not no_selection["ok"] and rig.effects() == before
    result = service.call(
        "task_admit",
        {
            "discovery_ref": inspected["discovery_ref"],
            "writing_ref": approved["approval_ref"],
            "selection_ref": rig.select(inspected["discovery_ref"]),
            "operation_key": "ordered-admission",
        },
    )
    task = rig.core.get_task(rig.handle, result["data"]["id"])
    after_admission = rig.effects()
    denied = service.call(
        "worker_start",
        {
            "task_id": task.id,
            "host": "fixture",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": "start-before-queue",
        },
    )
    assert not denied["ok"] and rig.effects() == after_admission
