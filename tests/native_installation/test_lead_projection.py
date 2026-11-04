"""Declared installed fixtures for current public event projection boundaries."""

import json

import pytest

from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.hosts.bridge.public_output import TerminalOutput
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native import lead_startup
from ai_task_manager.installation.native.controller import NormalInvocationController
from tests.native_installation import test_lead_startup

lead = test_lead_startup.lead


def quarantine(service, event):
    task = service.core.get_task(service.handle, event.task_id)
    journal = service.core.context.principals
    with journal.transaction() as conn:
        proof = journal._proof(
            conn,
            "external_observation",
            service.scope,
            task.id,
            {
                "issue_id": task.issue_id,
                "source_revision": "9999-reassigned",
                "status": "Backlog",
                "assigned_to_owner": False,
            },
        )
    service.core.external.observe(
        service.handle, task.id, task.version, task.decision_revision, proof, "reassigned"
    )
    projected = service.call("event_list", {})
    selected = next(e for e in projected["data"] if e["id"] == event.id)
    assert selected["payload"] == {"quarantined": True, "ownership_reconciliation_required": True}
    assert service.core.notifications.list_events(service.handle)[0].payload == event.payload


@pytest.mark.parametrize("boundary", ["launch", "input", "late_input", "publication"])
def test_LP01_reassigned_event_refused_at_effect_boundary(lead, monkeypatch, boundary):
    p, b, store, manifest, service, event = lead
    publications = []
    original_publish = TerminalOutput.publish

    def publish(self, output):
        publications.append(output.event_id)
        return original_publish(self, output)

    monkeypatch.setattr(TerminalOutput, "publish", publish)
    if boundary == "launch":
        quarantine(service, event)
    elif boundary == "input":
        original = OperatorChannel.capture_enrollment

        def capture(self, **kwargs):
            result = original(self, **kwargs)
            quarantine(service, event)
            return result

        monkeypatch.setattr(OperatorChannel, "capture_enrollment", capture)
    elif boundary == "late_input":
        original = lead_startup.LeadAccounting.event_turn

        def event_turn(self, event_id):
            result = original(self, event_id)
            quarantine(service, event)
            return result

        monkeypatch.setattr(lead_startup.LeadAccounting, "event_turn", event_turn)
    else:
        original = lead_startup.LeadAccounting.record

        def record(self, permit, **kwargs):
            result = original(self, permit, **kwargs)
            if kwargs.get("outcome") == "observed_turn":
                quarantine(service, event)
            return result

        monkeypatch.setattr(lead_startup.LeadAccounting, "record", record)
    with pytest.raises(AccessDenied, match="projected event"):
        lead_startup.report(p.config, b.installation)
    with service.core.context.db.transaction() as conn:
        stages = {
            r[0] for r in conn.execute("SELECT stage FROM receipts WHERE event_id=?", (event.id,))
        }
    assert publications == []
    assert "user_reported" not in stages
    if boundary != "publication":
        assert stages == {"queued"}
        audit = b.root / "process" / "native-input.jsonl"
        rows = (
            [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        )
        assert not any("assignment:lead-event" in row["call_id"] for row in rows)
    permission = store.permission(manifest.data["authorization_ref"])
    calls = NormalInvocationController(store, permission).snapshot()["calls"]
    assert all(row["process_stopped"] for row in calls)
    if boundary == "launch":
        assert calls == []
        assert not (b.root / "process").exists()


def test_LP02_missing_projected_event_refuses_before_launch(lead, monkeypatch):
    p, b, store, manifest, service, event = lead
    original = ApplicationService.call

    def call(self, operation, data):
        result = original(self, operation, data)
        if operation == "event_list":
            result["data"] = [item for item in result["data"] if item["id"] != event.id]
        return result

    monkeypatch.setattr(ApplicationService, "call", call)
    with pytest.raises(AccessDenied, match="projected event"):
        lead_startup.report(p.config, b.installation)
    assert not (b.root / "process").exists()
