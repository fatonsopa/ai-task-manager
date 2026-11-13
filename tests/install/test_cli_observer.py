"""Audit observer controls count attempted dispatches without credentials or network effects."""

import json
import time
from uuid import uuid4

import pytest

from ai_task_manager.config.files import read_private
from tests.install.cli_observer import DispatchObserver, open_ledger
from tests.linear.helpers import EXPECTED
from tests.linear.live.ledger import DriverRefused, Limits, RunLedger, Scope


def ledger(tmp_path):
    now = time.time()
    return RunLedger(
        tmp_path / "ledger.sqlite",
        str(uuid4()),
        Scope(
            EXPECTED.workspace_id,
            EXPECTED.viewer_id,
            EXPECTED.team_id,
            EXPECTED.project_id,
            str(uuid4()),
        ),
        Limits(
            now + 300,
            requests=5,
            projects=1,
            issues=1,
            webhooks=0,
            comments=0,
            started_at=now,
            cleanup_requests=1,
            cleanup_seconds=30,
        ),
    )


def test_OB01_exact_default_url_charges_attempts_and_ignores_credentials(tmp_path):
    run = ledger(tmp_path)
    receipt = tmp_path / "receipt.json"
    endpoint = "https://api.linear.app/graphql"
    observer = DispatchObserver(run, endpoint, receipt, {"mode": "genuine_default_provider"})
    raw = json.dumps(
        {
            "query": "query ReadIssue { issue { id } }",
            "variables": {"id": str(uuid4()), "title": "PRIVATE_PAYLOAD_TEXT"},
        }
    ).encode()
    for _ in range(4):
        observer.audit(
            "urllib.Request", (endpoint, raw, {"Authorization": "SECRET_HEADER_VALUE"}, "POST")
        )
    assert len(run.summary()["requests"]) == 4
    observed = read_private(receipt)
    assert [r["index"] for r in observed["dispatches"]] == [1, 2, 3, 4]
    assert all(
        set(r) == {"index", "operation", "query_sha256", "variables_sha256"}
        for r in observed["dispatches"]
    )
    assert "SECRET_HEADER_VALUE" not in receipt.read_text()
    assert "PRIVATE_PAYLOAD_TEXT" not in receipt.read_text()
    with pytest.raises(DriverRefused, match="request_budget_exhausted"):
        observer.audit("urllib.Request", (endpoint, raw, {"Authorization": "SECRET"}, "POST"))
    assert read_private(receipt) == observed
    assert len(run.summary()["requests"]) == 4


def test_OB02_foreign_url_refuses_before_charging_and_original_limits_survive(tmp_path):
    run = ledger(tmp_path)
    reopened = open_ledger(run.path)
    assert (
        reopened.run_id == run.run_id
        and reopened.scope == run.scope
        and reopened.limits == run.limits
    )
    observer = DispatchObserver(
        reopened, "https://api.linear.app/graphql", tmp_path / "receipt.json", {}
    )
    with pytest.raises(DriverRefused, match="unexpected_product_dispatch"):
        observer.audit("urllib.Request", ("https://foreign.invalid", b"{}", {}, "POST"))
    assert reopened.summary()["requests"] == []


def test_OB03_existing_receipt_is_retained_without_another_attempt(tmp_path):
    run = ledger(tmp_path)
    receipt = tmp_path / "receipt.json"
    DispatchObserver(run, "https://api.linear.app/graphql", receipt, {"original": True})
    before = receipt.read_bytes()
    with pytest.raises(DriverRefused, match="new_observer_receipt_required"):
        DispatchObserver(run, "https://api.linear.app/graphql", receipt, {"replacement": True})
    assert receipt.read_bytes() == before
    assert run.summary()["requests"] == []
