from dataclasses import asdict, replace

import pytest

from ai_task_manager.adapters.decisions.authority import HostObservation
from ai_task_manager.application.service.models import SCHEMAS
from ai_task_manager.domains.work.models import Scope, SessionHandle

from .helpers import ServiceRig


def test_a04_service_scopes_all_reads_and_pending_intake_is_readable(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.task(confirmed=False)
    foreign = env.rig.session(replace(env.rig.scope, logical_session="other"))
    foreign_task = env.rig.task(handle=foreign, confirmed=False)
    response = env.service.call("task_list", {})
    assert response["ok"] and len(response["data"]) == 1
    assert response["data"][0]["id"] == task.id
    assert response["data"][0]["sync_state"] == "pending"
    assert env.service.call("task_show", {"task_id": task.id})["ok"]
    assert env.service.call("task_show", {"task_id": foreign_task.id})["outcome"] == "denied"
    env.rig.core.revoke_session(env.rig.handle)
    assert env.service.call("task_list", {})["outcome"] == "denied"


@pytest.mark.parametrize("operation", list(SCHEMAS))
def test_a12_a23_no_unknown_authority_fields(operation, tmp_path):
    env = ServiceRig(tmp_path)
    response = env.service.call(operation, {"human": True, "reviewed": True, "principal": "other"})
    assert not response["ok"] and response["outcome"] == "invalid_request"
    assert env.rig.core.list_tasks(env.rig.handle) == []


def test_a13_delegation_enqueues_but_cannot_claim_human_acceptance(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.task()
    response = env.service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": "enqueue",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": "enqueue-once",
        },
    )
    assert response["ok"] and response["data"]["observed_status"] == "Todo"
    task = env.rig.current(task)
    review = env.service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": "request_review",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": "fake-human",
        },
    )
    assert review["outcome"] == "denied"
    assert env.rig.current(task).observed_status == "Todo"
    env.observations.grants["host-grant"] = replace(
        env.observations.grants["host-grant"], actions=()
    )
    denied = env.service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": "dequeue",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": "no-grant",
        },
    )
    assert denied["outcome"] == "denied"


def test_a16_stale_versions_leave_no_partial_task_change(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.task()
    old = asdict(task)
    response = env.service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": "enqueue",
            "expected_version": task.version - 1,
            "decision_revision": task.decision_revision,
            "operation_key": "stale",
        },
    )
    assert response["outcome"] == "invalid_request"
    assert asdict(env.rig.current(task)) == old
    assert env.provider.calls == []


def test_a17_missing_model_bridge_is_a_refusal_without_process_effect(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.queued()
    response = env.service.call(
        "worker_start",
        {
            "task_id": task.id,
            "host": "claude",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "operation_key": "missing-model-bridge",
        },
    )
    assert response["outcome"] == "blocked"
    assert env.rig.current(task).execution_state == "idle"


def test_a19_notification_receipts_need_actual_separate_observations(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.task()
    event = env.rig.core.notifications.list_events(env.rig.handle)[0]
    env.service.call("event_list", {})
    assert set(env.rig.core.notifications.receipts(env.rig.handle, event.id)) == {"queued"}
    payload = {
        "event_id": event.id,
        "stage": "ai_acknowledged",
        "observation_ref": "invented",
        "operation_key": "invented",
    }
    assert env.service.call("event_acknowledge", payload)["outcome"] == "denied"
    for index, stage in enumerate(("delivered", "ai_acknowledged", "user_reported")):
        ref = "observed-" + stage
        env.observations.host[ref] = HostObservation(
            env.rig.scope, event.id, env.service.session_id, stage, "actual-fixture-trace-" + stage
        )
        response = env.service.call(
            "event_acknowledge",
            {
                **payload,
                "stage": stage,
                "observation_ref": ref,
                "operation_key": "receipt-" + stage,
            },
        )
        assert response["ok"]
        assert len(env.rig.core.notifications.receipts(env.rig.handle, event.id)) == index + 2
    assert env.rig.current(task).observed_status == "Backlog"


def test_a19_stage_skip_and_foreign_trace_rejected(tmp_path):
    env = ServiceRig(tmp_path)
    env.rig.task()
    event = env.rig.core.notifications.list_events(env.rig.handle)[0]
    for name, scope in [
        ("skip", env.rig.scope),
        ("foreign", Scope("linear", "x", "y", "z", "other")),
    ]:
        env.observations.host[name] = HostObservation(
            scope, event.id, env.service.session_id, "user_reported", "fixture"
        )
        result = env.service.call(
            "event_acknowledge",
            {
                "event_id": event.id,
                "stage": "user_reported",
                "observation_ref": name,
                "operation_key": name,
            },
        )
        assert not result["ok"]
    assert set(env.rig.core.notifications.receipts(env.rig.handle, event.id)) == {"queued"}


def test_a02_forged_session_and_raw_status_operation_are_unavailable(tmp_path):
    env = ServiceRig(tmp_path)
    assert not env.service.call("set_status", {"status": "Done"})["ok"]
    with pytest.raises(Exception):
        type(env.service)(env.rig.core, SessionHandle("fabricated"))
