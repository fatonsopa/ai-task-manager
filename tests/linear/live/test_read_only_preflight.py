"""Authored read-only transport checks; no live account evidence."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import Credentials
from ai_task_manager.adapters.linear.transport import HttpTransport

from .fixture_graphql import OPERATIONS, SHAPE
from .ledger import DriverRefused
from .read_only_preflight import ReadOnlyPreflight

pytest_plugins = ["tests.linear.live.test_actual_adapter"]


def preflight(adapter):
    _, _, ledger, provider, *_ = adapter
    return ReadOnlyPreflight(
        ledger,
        HttpTransport(timeout=2, _test_endpoint=provider.endpoint),
        lambda: Credentials("AUTHORED_NOT_LIVE"),
    )


def test_pf01_actual_authored_http_read_only_preflight_records_two_reads(adapter):
    _, _, ledger, provider, *_ = adapter
    result = preflight(adapter).run()
    assert [request[0]["query"] for request in provider.requests] == [
        SHAPE,
        OPERATIONS["identity"],
    ]
    assert result.run_id == ledger.run_id
    assert result.actual_identity["viewer"] == {"id": ledger.scope.owner, "admin": True}
    assert result.actual_identity["organization"] == {"id": ledger.scope.workspace}
    assert result.actual_identity["team"] == {"id": ledger.scope.team, "key": "TST"}
    assert len(result.requests) == len(ledger.summary()["requests"]) == 2
    assert all(fact["status"] == 200 for fact in result.requests)
    assert not provider.projects and not provider.webhooks
    assert not ledger.summary()["operations"] and not ledger.summary()["resources"]
    assert "AUTHORED_NOT_LIVE" not in json.dumps(asdict(result))
    assert b"AUTHORED_NOT_LIVE" not in ledger.path.read_bytes()


def test_pf02_read_only_run_cannot_repeat_or_create(adapter):
    _, _, ledger, provider, *_ = adapter
    run = preflight(adapter)
    run.run()
    before = ledger.summary()
    with pytest.raises(DriverRefused, match="already_attempted"):
        run.run()
    assert ledger.summary() == before and len(provider.requests) == 2
    assert not hasattr(run, "create_project") and not hasattr(run, "create_webhook")


@pytest.mark.parametrize("target", ["order", "variables", "mutation", "duplicate"])
def test_pf03_fixed_transport_refuses_wrong_order_variables_or_mutation(adapter, target):
    _, _, _, provider, *_ = adapter
    run = preflight(adapter)
    query, variables = SHAPE, {}
    if target == "order":
        query, variables = OPERATIONS["identity"], {"team": str(uuid4())}
    elif target == "variables":
        variables = {"unexpected": "value"}
    elif target == "mutation":
        query, variables = OPERATIONS["project_create"], {"input": {"id": str(uuid4())}}
    else:
        run._transport.send(SHAPE, {}, Credentials("AUTHORED_NOT_LIVE"))
    before = len(provider.requests)
    with pytest.raises(DriverRefused, match="sequence_required"):
        run._transport.send(query, variables, Credentials("AUTHORED_NOT_LIVE"))
    assert len(provider.requests) == before
    assert not provider.projects and not provider.webhooks


@pytest.mark.parametrize("target", ["schema", "viewer"])
def test_pf04_schema_or_identity_failure_has_no_effect(adapter, target):
    _, _, ledger, provider, *_ = adapter

    def change(query, variables, result):
        if target == "schema" and query == SHAPE:
            result["t0"]["fields"] = []
        elif target == "viewer" and query == OPERATIONS["identity"]:
            result["viewer"]["id"] = str(uuid4())
        return result

    provider.transform = change
    with pytest.raises(DriverRefused):
        preflight(adapter).run()
    assert len(provider.requests) == (1 if target == "schema" else 2)
    assert not provider.projects and not provider.webhooks
    assert not ledger.summary()["operations"]


def test_pf05_actual_http_auth_failure_reports_no_secret(adapter):
    _, _, ledger, provider, *_ = adapter
    provider.fail_next = (403, b"PRIVATE_AUTHORED_ERROR")
    with pytest.raises(LinearError) as caught:
        preflight(adapter).run()
    assert "PRIVATE" not in str(caught.value)
    assert len(provider.requests) == len(ledger.summary()["requests"]) == 1
    assert not provider.projects and not provider.webhooks


def test_pf06_concurrent_call_has_single_sequence(adapter):
    _, _, ledger, provider, *_ = adapter
    run = preflight(adapter)
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(1)
        try:
            return run.run()
        except DriverRefused as error:
            assert str(error) == "read_only_preflight_already_attempted"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(provider.requests) == len(ledger.summary()["requests"]) == 2
