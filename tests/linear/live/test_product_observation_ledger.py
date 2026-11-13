"""Post-effect fixture ownership never fabricates a product create/send history."""

import time
from uuid import uuid4

import pytest

from tests.linear.helpers import EXPECTED

from .ledger import DriverRefused, Limits, RunLedger, Scope


def configured(tmp_path):
    started = time.time()
    return RunLedger(
        tmp_path / "run.sqlite",
        str(uuid4()),
        Scope(
            EXPECTED.workspace_id,
            EXPECTED.viewer_id,
            EXPECTED.team_id,
            EXPECTED.project_id,
            str(uuid4()),
        ),
        Limits(
            started + 300,
            requests=10,
            projects=1,
            issues=1,
            webhooks=0,
            comments=0,
            started_at=started,
            cleanup_requests=2,
            cleanup_seconds=30,
        ),
    )


def test_PO01_post_effect_observation_is_immutable_idempotent_and_not_a_send(tmp_path):
    ledger = configured(tmp_path)
    issue = str(uuid4())
    task = str(uuid4())
    evidence = {"task_id": task, "outbox_id": str(uuid4()), "approved_content": "a" * 64}
    first = ledger.observe_product_issue("observed-product:submit", issue, evidence)
    assert ledger.observe_product_issue("observed-product:submit", issue, evidence) == first
    summary = ledger.summary()
    assert summary["operations"] == [] and summary["requests"] == []
    assert summary["product_observations"][0]["origin"] == "confirmed_product_post_effect"
    assert len(summary["resources"]) == 1
    reopened = RunLedger(ledger.path, ledger.run_id, ledger.scope, ledger.limits)
    assert reopened.summary() == summary
    for key, identity, changed in [
        ("observed-product:submit", str(uuid4()), evidence),
        ("observed-product:submit", issue, {**evidence, "task_id": str(uuid4())}),
        ("changed-operation", issue, evidence),
    ]:
        with pytest.raises(DriverRefused):
            reopened.observe_product_issue(key, identity, changed)
    assert reopened.summary() == summary


@pytest.mark.parametrize("observation_first", [False, True])
def test_PO02_one_shared_issue_budget_covers_reserved_and_observed_resources(
    tmp_path, observation_first
):
    ledger = configured(tmp_path)
    if observation_first:
        ledger.observe_product_issue("observed", str(uuid4()), {"verified": "post-effect"})
        with pytest.raises(DriverRefused, match="resource_budget_exhausted"):
            ledger.begin("fixture-create", "issue", str(uuid4()), "a" * 64)
    else:
        ledger.begin("fixture-create", "issue", str(uuid4()), "a" * 64)
        with pytest.raises(DriverRefused, match="resource_budget_exhausted"):
            ledger.observe_product_issue("observed", str(uuid4()), {"verified": "post-effect"})
    assert (
        len(ledger.summary()["operations"]) + len(ledger.summary().get("product_observations", []))
        == 1
    )
