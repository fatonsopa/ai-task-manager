"""Authored loopback/Core controls; these do not claim genuine provider execution."""

import copy
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.application.linear_sync.outbound import OutboundDrain
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import EvidenceRequired, WorkError
from ai_task_manager.installation.workflows.bootstrap import prepare
from tests.complete_cycle.provider import CycleProvider
from tests.complete_cycle.test_pending_intake_repair import approve
from tests.module_core.fixture import Demonstration

BODY = """## Description

Prepare the recovery checkpoint.

## Expected outcome

Preserve the source and original checkpoint.

## Acceptance criteria

* Retain the original checkpoint after interruption.
"""


@pytest.fixture
def demo(tmp_path, monkeypatch):
    root = tmp_path / "scenario"
    root.mkdir(mode=0o700)
    database = tmp_path / "core.sqlite"
    database.touch(mode=0o600)
    with tempfile.TemporaryDirectory(prefix="eq-", dir="/tmp") as sockets:
        control = {
            "record": str(Path(sockets).resolve() / "owner.json"),
            "endpoint": str(Path(sockets).resolve() / "owner.sock"),
            "instance": str(uuid4()),
        }
        value = Demonstration(
            root, database, Path(os.environ["AITM_TEST_WRITER_ROOT"]),
            workflow_control=control, provider_factory=CycleProvider,
            capacity_policies=[{"host": "claude", "host_limit": 2, "project_limit": 2}],
        )
        monkeypatch.setattr(HttpTransport, "ENDPOINT", value.endpoint)
        try:
            yield value
        finally:
            value.close()


def task(demo, key):
    approval = approve(demo, BODY, code=False, units=("checkpoint",))
    result = prepare(demo.config, {
        "operation": "task_submit", "data": {"writing_ref": approval, "operation_key": key},
    })
    assert result["ok"]
    return result["data"]


def enqueue(value, key):
    return {"operation": "task_action", "data": {
        "task_id": value["id"], "expected_version": value["version"],
        "decision_revision": value["decision_revision"], "action": "enqueue",
        "operation_key": key,
    }}


def mutations(demo):
    return sum(request["query"].lstrip().startswith("mutation ")
               for request, _ in demo.provider.requests)


def lose_reply(demo, request, monkeypatch):
    original = LinearClient.update_issue

    def lost(client, value, kind):
        original(client, value, kind)
        raise LinearError("authored_lost_reply", uncertain=True)

    with monkeypatch.context() as patch:
        patch.setattr(LinearClient, "update_issue", lost)
        result = prepare(demo.config, request)
    assert not result["ok"] and result["outcome"] == "uncertain"


def test_original_enqueue_reconciles_lost_reply_then_replays_without_send(demo, monkeypatch):
    value = task(demo, "submit")
    request = enqueue(value, "enqueue-original")
    lose_reply(demo, request, monkeypatch)
    service = load_service(demo.config, workflow_factory=lambda *_: None)
    before = copy.deepcopy(service.provider.journal.sends(service.scope))
    count = mutations(demo)
    result = prepare(demo.config, request)
    assert result["ok"] and result["data"]["observed_status"] == "Todo"
    assert result["data"]["sync_state"] == "confirmed"
    again = prepare(demo.config, request)
    assert again["data"] == result["data"] and mutations(demo) == count
    sends = service.provider.journal.sends(service.scope)
    assert [s["id"] for s in sends] == [s["id"] for s in before]
    assert sends[-1]["state"] == "confirmed" and sends[-1]["attempt"] == 1


def test_changed_enqueue_key_cannot_use_another_requests_readback(demo, monkeypatch):
    request = enqueue(task(demo, "submit"), "enqueue-original")
    lose_reply(demo, request, monkeypatch)
    changed = copy.deepcopy(request)
    changed["data"]["operation_key"] = "foreign-key"
    before = mutations(demo)
    with pytest.raises(EvidenceRequired):
        prepare(demo.config, changed)
    assert mutations(demo) == before


def test_unrelated_pending_task_keeps_original_enqueue_fenced(demo, monkeypatch):
    first, second = task(demo, "submit-a"), task(demo, "submit-b")
    request = enqueue(first, "enqueue-a")
    lose_reply(demo, request, monkeypatch)
    service = load_service(demo.config, workflow_factory=lambda *_: None)
    with monkeypatch.context() as patch:
        patch.setattr(OutboundDrain, "run_once", lambda *args, **kwargs: None)
        assert not service.call("task_action", enqueue(second, "enqueue-b")["data"])["ok"]
    before = mutations(demo)
    with pytest.raises(EvidenceRequired):
        prepare(demo.config, request)
    assert mutations(demo) == before


def test_committed_but_unsent_enqueue_cannot_use_readback_route(demo, monkeypatch):
    request = enqueue(task(demo, "submit"), "enqueue-original")
    with monkeypatch.context() as patch:
        patch.setattr(OutboundDrain, "run_once", lambda *args, **kwargs: None)
        assert not prepare(demo.config, request)["ok"]
    before = mutations(demo)
    with pytest.raises(EvidenceRequired):
        prepare(demo.config, request)
    assert mutations(demo) == before


def test_missing_invocation_history_is_not_recreated(demo, monkeypatch):
    request = enqueue(task(demo, "submit"), "enqueue-original")
    lose_reply(demo, request, monkeypatch)
    service = load_service(demo.config, workflow_factory=lambda *_: None)
    with service.decisions.journal.transaction() as conn:
        conn.execute("DELETE FROM invocations")
    before = mutations(demo)
    with pytest.raises(EvidenceRequired):
        prepare(demo.config, request)
    with service.decisions.journal.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
    assert mutations(demo) == before


def test_revoked_enrollment_cannot_reconcile_an_original_enqueue(demo, monkeypatch):
    request = enqueue(task(demo, "submit"), "enqueue-original")
    lose_reply(demo, request, monkeypatch)
    service = load_service(demo.config, workflow_factory=lambda *_: None)
    service.decisions.observations.revoke()
    before = mutations(demo)
    with pytest.raises((EvidenceRequired, WorkError)):
        prepare(demo.config, request)
    assert mutations(demo) == before


@pytest.mark.parametrize("missing", ["journal", "key"])
def test_original_enqueue_refuses_missing_writer_history_without_recreation(
    demo, monkeypatch, missing
):
    request = enqueue(task(demo, "submit"), "enqueue-original")
    lose_reply(demo, request, monkeypatch)
    import json

    config = json.loads(demo.config.read_text())
    original = Path(config["writer"][missing])
    retained = original.with_name(original.name + ".retained")
    original.rename(retained)
    before = mutations(demo)
    with pytest.raises((OSError, WorkError)):
        prepare(demo.config, request)
    assert retained.exists() and not original.exists()
    assert mutations(demo) == before


def test_initial_writer_preparation_after_setup_remains_available(demo):
    request = {
        "operation": "writing_prepare",
        "data": {
            "kind": "issue_chore", "purpose": "task", "title": "Retain the checkpoint",
            "body": BODY, "code_bearing": False, "required_work": ["checkpoint"],
        },
    }
    result = prepare(demo.config, request)
    assert result["ok"] and result["data"]["mechanical_passed"]
    assert mutations(demo) == 0
