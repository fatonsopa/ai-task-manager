"""Authenticated loopback scope changes and retained runtime ownership controls."""

import json
from threading import Event

import pytest

from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.application.service.projection import attempt_view
from ai_task_manager.presentation.cli.render import task_lines
from tests.application.helpers import ServiceRig
from tests.linear.helpers import LinearRig, ProviderFixture, uid


@pytest.mark.parametrize("change", ["assignee", "project", "team", "remove"])
def test_a32_authenticated_scope_exit_quarantines_without_releasing_ownership(tmp_path, change):
    provider = ProviderFixture()
    try:
        rig = LinearRig(tmp_path, provider)
        task, attempt = rig.running()
        service = ApplicationService(rig.core, rig.handle)
        before = rig.core.get_attempt(rig.handle, attempt.id)
        changes = {"title": "Foreign current title", "description": "Foreign current body"}
        if change != "remove":
            changes[change] = {"id": uid(999), **({"key": "OTHER"} if change == "team" else {})}
        provider.edit(task.issue_id, **changes)
        headers, raw = rig.signed(
            provider.event(task.issue_id, action="remove" if change == "remove" else "update")
        )
        wake = Event()
        rig.receiver.wake = wake.set
        requests_before = len(provider.requests)
        assert rig.receiver.receive("POST", headers, raw).status == 200
        assert wake.is_set()
        # Receiver durably accepts and signals only: no provider read/drain on HTTP path.
        assert len(provider.requests) == requests_before
        assert rig.core.get_task(rig.handle, task.id).sync_state == "confirmed"
        assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
        listed = service.call("task_list", {})
        assert listed["ok"] and listed["data"] == [] and listed["quarantined_count"] == 1
        assert "reconciliation" in task_lines([], quarantined_count=1)
        shown = service.call("task_show", {"task_id": task.id})
        assert shown["data"]["quarantined"] and shown["data"]["active_group"] is False
        assert shown["data"]["execution_state"] == "quarantined"
        assert shown["data"]["body"] is None and shown["data"]["observed_status"] is None
        assert shown["data"]["resource_reconciliation_required"]
        public = json.dumps(
            [
                shown,
                service.call("task_external_changes", {"task_id": task.id}),
                service.call("event_list", {}),
            ]
        )
        assert "Foreign current" not in public
        retained = rig.core.get_attempt(rig.handle, attempt.id)
        assert retained == before and retained.state == "running"
        assert retained.worktree_id and retained.database_id
        assert attempt_view(service, retained)["state"] == "quarantined"
        current = rig.core.get_task(rig.handle, task.id)
        requests = {
            "task_action": {
                "task_id": task.id,
                "action": "hold",
                "expected_version": current.version,
                "decision_revision": current.decision_revision,
                "operation_key": "exit-hold",
            },
            "worker_start": {
                "task_id": task.id,
                "host": "fixture",
                "expected_version": current.version,
                "decision_revision": current.decision_revision,
                "operation_key": "exit-start",
            },
            "worker_execute": {
                "attempt_id": attempt.id,
                "command_ref": "unavailable",
                "operation_key": "exit-execute",
            },
            "task_progress": {
                "task_id": task.id,
                "writing_ref": "unavailable",
                "operation_key": "exit-progress",
            },
        }
        # A configured synthetic decision journal is necessary for keyed mutations;
        # no public endpoint creates authority while checking ownership.
        service.decisions = ServiceRig(tmp_path / "authority-fixture").decisions
        for operation, arguments in requests.items():
            denied = service.call(operation, arguments)
            assert denied["outcome"] == "denied", (operation, denied)
        assert rig.core.get_attempt(rig.handle, attempt.id) == before
    finally:
        provider.close()


def test_a32_unsettled_active_task_is_not_presented_as_authorized_running_work(tmp_path):
    env = ServiceRig(tmp_path)
    task, attempt = env.rig.running()
    proof = env.rig.proof(
        "external_observation",
        task.id,
        {
            "issue_id": task.issue_id,
            "source_revision": "2026-11-01T12:00:00Z",
            "status": "On Hold",
            "title": task.title,
            "body": task.body,
            "assigned_to_owner": True,
            "actor_kind": "human",
            "actor_authorized": True,
        },
    )
    env.rig.core.external.observe(
        env.rig.handle, task.id, task.version, task.decision_revision, proof, "observed-hold"
    )
    shown = env.service.call("task_show", {"task_id": task.id})["data"]
    assert shown["execution_state"] == "stopping_or_unresolved"
    assert shown["last_observed_execution_state"] == "running" and shown["work_authorized"] is False
    assert env.service.call("task_list", {})["quarantined_count"] == 0
    assert env.rig.core.get_attempt(env.rig.handle, attempt.id).state == "running"
