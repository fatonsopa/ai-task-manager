"""Actual local HTTP, actual core and actual Writer; no live provider/model."""

import os

from ai_task_manager.application.service.linear import CredentialFile
from tests.application.helpers import FEATURE_BODY, reviewed_control
from tests.linear.helpers import WEBHOOK, uid


def approve(service, title, body, *, purpose="task", task_id=None):
    data = {
        "kind": "issue_feature",
        "purpose": purpose,
        "title": title,
        "body": body,
        "code_bearing": True,
        "required_work": ["complete"],
    }
    if task_id:
        data["task_id"] = task_id
    prepared = service.call("writing_prepare", data)
    assert prepared["ok"], prepared
    packet = prepared["data"]["packet"]
    assert prepared["data"]["mechanical_passed"]
    reviewed = service.call(
        "writing_review",
        {"writing_id": prepared["data"]["writing_id"], "review": reviewed_control(packet)},
    )
    assert reviewed["ok"] and reviewed["data"]["accepted"], reviewed
    finalized = service.call("writing_finalize", {"writing_id": prepared["data"]["writing_id"]})
    assert finalized["ok"], finalized
    return finalized["data"]["approval_ref"]


def test_a27_real_writer_immediate_http_intake_queue_and_reviewed_progress(composed):
    service, rig, provider, _ = composed
    ref = approve(service, "Verify scoped task ownership", FEATURE_BODY)
    submitted = service.call(
        "task_submit", {"writing_ref": ref, "operation_key": "composed-intake"}
    )
    assert submitted["ok"] and submitted["data"]["observed_status"] == "Backlog", submitted
    task = service.core.get_task(service.handle, submitted["data"]["id"])
    assert provider.issues[task.issue_id]["state"]["name"] == "Backlog"
    assert len(provider.issues) == 1
    replay = service.call("task_submit", {"writing_ref": ref, "operation_key": "composed-intake"})
    assert replay["ok"] and len(provider.issues) == 1
    queued = service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": "enqueue",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": "composed-queue",
        },
    )
    assert queued["ok"] and queued["data"]["observed_status"] == "Todo", queued
    progress = approve(
        service,
        "ignored progress title",
        "The scoped check passed. Merge and deployment evidence remain pending.",
        purpose="progress",
        task_id=task.id,
    )
    published = service.call(
        "task_progress",
        {"task_id": task.id, "writing_ref": progress, "operation_key": "composed-progress"},
    )
    assert published["ok"] and published["data"]["state"] == "confirmed", published
    assert len(provider.comments) == 1
    assert service.core.get_task(service.handle, task.id).observed_status == "Todo"
    event = service.core.notifications.list_events(service.handle)[-1]
    assert event.kind == "provider_progress_publication_confirmed"
    assert set(service.core.notifications.receipts(service.handle, event.id)) == {"queued"}
    assert not service.call(
        "task_submit", {"writing_ref": progress, "operation_key": "progress-is-not-work"}
    )["ok"]


def test_a29_external_prose_stays_staged_until_actual_writer_and_fresh_readback(composed):
    service, rig, provider, components = composed
    ref = approve(service, "Verify scoped task ownership", FEATURE_BODY)
    response = service.call("task_submit", {"writing_ref": ref, "operation_key": "external-intake"})
    task = service.core.get_task(service.handle, response["data"]["id"])
    new_title, new_body = (
        "Verify transferred task ownership",
        # Persisted Linear document bytes follow the v2 terminal-LF contract.
        FEATURE_BODY.replace("Separate sessions", "Transferred sessions").removesuffix("\n"),
    )
    provider.edit(task.issue_id, title=new_title, description=new_body)
    from ai_task_manager.adapters.linear.webhooks import WebhookReceiver

    receiver = WebhookReceiver(
        service.core,
        service.handle,
        components.journal,
        components.binding,
        WEBHOOK,
        lambda: b"fixture-webhook-signing-secret",
        clock=lambda: rig.now,
    )
    headers, body = rig.signed(provider.event(task.issue_id))
    assert receiver.receive("POST", headers, body).status == 200
    assert service.inbound.run_once(service.handle)["state"] == "conflict"
    current = service.core.get_task(service.handle, task.id)
    assert current.body != new_body and current.human_hold
    staged = service.call("task_external_changes", {"task_id": task.id})
    assert staged["ok"] and staged["data"][-1]["state"] == "pending"
    approved = approve(service, new_title, new_body)
    acceptance = {
        "task_id": task.id,
        "observation_id": staged["data"][-1]["id"],
        "writing_ref": approved,
        "expected_version": current.version,
        "decision_revision": current.decision_revision,
        "operation_key": "external-approval",
    }
    accepted = service.call("task_accept_external_prose", acceptance)
    assert accepted["ok"] and accepted["data"]["body"] == new_body, accepted
    assert accepted["data"]["human_hold"] and accepted["data"]["sync_state"] == "uncertain"
    assert provider.issues[task.issue_id]["description"] == new_body
    provider.edit(task.issue_id, project={"id": uid(999)}, title="Foreign current title")
    headers, body = rig.signed(provider.event(task.issue_id))
    assert receiver.receive("POST", headers, body).status == 200
    assert service.inbound.run_once(service.handle)["state"] == "conflict"
    replay = service.call("task_accept_external_prose", acceptance)
    assert replay["ok"] and replay["data"]["quarantined"]
    assert replay["data"]["body"] is None


def test_credential_file_is_data_and_never_shell_code(tmp_path):
    path = tmp_path / "fake.env"
    sentinel = tmp_path / "must-not-exist"
    path.write_text("LINEAR_API_KEY='$(touch " + str(sentinel) + ")'\n")
    os.chmod(path, 0o600)
    assert CredentialFile(path, "LINEAR_API_KEY")().value.startswith("$(touch ")
    assert not sentinel.exists()
