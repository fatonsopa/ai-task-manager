"""Real installed-enrollment selection source with an authored HTTP provider."""

import json
import time
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.sessions import setup
from tests.linear.helpers import CONNECTION, EXPECTED


def configured(tmp_path, provider, monkeypatch, *, delegated=True, name="selected"):
    root = tmp_path / name
    root.mkdir(mode=0o700)
    monkeypatch.setattr(HttpTransport, "ENDPOINT", provider.endpoint)
    credential = root / "credential.env"
    credential.write_text("LINEAR_TEST_USER_A_TOKEN=authored-selection-only\n")
    credential.chmod(0o600)
    config = root / "service.json"
    write_private(
        config,
        {
            "version": 1,
            "core_database": str(root / "core.sqlite"),
            "authority_database": str(root / "authority.sqlite"),
            "authority_key": str(root / "authority.key"),
            "session_file": str(root / "session.json"),
            "approval_policy": {
                "writer_hash": "a" * 64,
                "reference_hash": "b" * 64,
                "style_hash": "c" * 64,
            },
            "capacity_policies": [{"host": "codex", "host_limit": 1, "project_limit": 1}],
            "linear": {
                "expected": asdict(EXPECTED),
                "agent_connection": asdict(replace(CONNECTION, logical_session=name)),
                "credential_file": str(credential),
                "credential_key": "LINEAR_TEST_USER_A_TOKEN",
                "journal": str(root / "provider.sqlite"),
            },
        },
    )
    actions = ("enqueue", "existing_issue_selection") if delegated else ("enqueue",)
    setup(
        config,
        logical_session=name,
        actions=actions,
        deadline=time.time() + 120,
        operation_key="authored-selection-setup",
    )
    return load_service(config)


def inspect(service, provider):
    issue = provider.seed()
    result = service.call("task_inspect", {"issue": issue})
    assert result["ok"], result
    return issue, {
        "discovery_ref": result["data"]["discovery_ref"],
        "operation_key": "select-current",
    }


def test_selection_source_is_exact_delegated_request_and_replay(tmp_path, provider, monkeypatch):
    service = configured(tmp_path, provider, monkeypatch)
    issue, request = inspect(service, provider)
    selected = service.call("task_select", request)
    assert selected["ok"] and selected["data"]["issue_id"] == issue, selected
    reference = selected["data"]["selection_ref"]
    proof = service.decisions.journal.verify_evidence(reference)
    assert proof.kind == "existing_issue_selection" and proof.scope == service.scope
    assert proof.payload["claim_store"] == service.claim_store.context
    assert service.call("task_select", request) == selected
    with service.decisions.journal.transaction() as conn:
        rows = conn.execute(
            "SELECT data FROM records WHERE ref LIKE 'session-issue-selection:%'"
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["invocation_id"]
    assert service.core.list_tasks(service.handle) == []
    assert not any("mutation" in row[0]["query"].lower() for row in provider.requests)


@pytest.mark.parametrize(
    "boundary",
    [
        "missing_delegation",
        "foreign",
        "expired",
        "revoked",
        "changed_request",
        "changed_account",
        "missing_agent",
        "self_authority",
    ],
)
def test_selection_refuses_missing_foreign_stale_or_changed_source(
    tmp_path, provider, monkeypatch, boundary
):
    service = configured(
        tmp_path, provider, monkeypatch, delegated=boundary != "missing_delegation"
    )
    _, request = inspect(service, provider)
    selected = None
    if boundary in {"revoked", "changed_request", "changed_account", "missing_agent"}:
        selected = service.call("task_select", request)
        assert selected["ok"], selected
    if boundary == "foreign":
        other = configured(tmp_path, provider, monkeypatch, name="other")
        _, other_request = inspect(other, provider)
        request["discovery_ref"] = other_request["discovery_ref"]
    elif boundary == "expired":
        service.existing.reader.journal.clock = lambda: time.time() + 1000000
    elif boundary == "revoked":
        service.decisions.observations.revoke()
        with pytest.raises(AccessDenied):
            service.decisions.journal.verify_evidence(selected["data"]["selection_ref"])
    elif boundary == "changed_request":
        _, changed = inspect(service, provider)
        request["discovery_ref"] = changed["discovery_ref"]
    elif boundary == "changed_account":
        provider.viewer_id = str(uuid4())
    elif boundary == "missing_agent":
        provider.agent_users = []
    elif boundary == "self_authority":
        request["selected"] = True
    refused = service.call("task_select", request)
    assert not refused["ok"], refused
    assert service.core.list_tasks(service.handle) == []
    assert not any("mutation" in row[0]["query"].lower() for row in provider.requests)


def test_selection_preserves_ordinary_owner_forwarding(tmp_path, provider, monkeypatch):
    service = configured(tmp_path, provider, monkeypatch)
    _, request = inspect(service, provider)
    calls = []

    class Owner:
        def forward(self, operation, data):
            calls.append((operation, data))
            return {"ok": False, "outcome": "denied", "data": None}

    service.workflows = Owner()
    assert service.call("task_select", request)["outcome"] == "denied"
    assert calls == [("task_select", request)]
    with service.decisions.journal.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
