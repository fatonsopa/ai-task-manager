"""Audit observer controls count attempted dispatches without credentials or network effects."""

import json
import time
from unittest.mock import patch
from uuid import uuid4

import pytest

from ai_task_manager.config.files import read_private, write_private
from tests.install.cli_observer import DispatchObserver, main, open_ledger
from tests.linear.helpers import EXPECTED
from tests.linear.live.ledger import DriverRefused, Limits, RunLedger, Scope


@pytest.fixture(autouse=True)
def close_owned_observer_tools(monkeypatch):
    created = []
    original = DispatchObserver.__init__

    def observed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(DispatchObserver, "__init__", observed)
    yield
    for observer in created:
        observer.close()


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


@pytest.mark.parametrize(
    ("group", "action"),
    [("workflow", "owner"), ("event", "acknowledge"), ("task", "cancel"), ("sync", "listen")],
)
def test_OB04_execution_and_receipt_commands_never_reach_installed_entry(tmp_path, group, action):
    store, receipt = tmp_path / "unopened.sqlite", tmp_path / "unwritten.json"
    with patch("runpy.run_path") as entry, pytest.raises(DriverRefused):
        main([
            "--ledger", str(store), "--receipt", str(receipt), "--", "--config",
            str(tmp_path / "unopened-service.json"), "--json", group, action,
        ])
    entry.assert_not_called()
    assert not store.exists() and not receipt.exists()


def test_OB05_normal_task_action_remains_enqueue_only(tmp_path):
    request = tmp_path / "cancel.json"
    write_private(request, {"action": "cancel"})
    with patch("runpy.run_path") as entry, pytest.raises(DriverRefused, match="enqueue_required"):
        main([
            "--ledger", str(tmp_path / "unopened.sqlite"), "--receipt",
            str(tmp_path / "unwritten.json"), "--", "--config",
            str(tmp_path / "unopened-service.json"), "--json", "task", "action", "--input",
            str(request),
        ])
    entry.assert_not_called()
    assert not (tmp_path / "unopened.sqlite").exists()
    assert not (tmp_path / "unwritten.json").exists()


@pytest.mark.parametrize(
    "status,category", [(200, None), (500, "http_failure"), (429, "rate_limited")]
)
def test_preparation_observer_retains_returned_http_before_client_failure(
    tmp_path, monkeypatch, status, category
):
    import io
    import sys

    from ai_task_manager.adapters.linear import transport as transport_module
    from ai_task_manager.adapters.linear.client import LinearClient
    from ai_task_manager.adapters.linear.errors import LinearError
    from ai_task_manager.adapters.linear.models import Credentials
    from ai_task_manager.adapters.linear.transport import HttpTransport
    from tests.linear.helpers import observed_quota, quota_headers

    run = ledger(tmp_path)
    receipt = tmp_path / "response-observer.json"
    observer = DispatchObserver(
        run, HttpTransport.ENDPOINT, receipt, {"label": "preparation-control"}
    )
    sys.addaudithook(observer.audit)
    code = "RATELIMITED" if status == 429 else "INTERNAL_SERVER_ERROR"
    body = (
        {"data": {"ok": True}}
        if status == 200
        else {
            "errors": [
                {"message": "PRIVATE_ERROR_BODY", "extensions": {"code": code}},
                {"extensions": {"code": "PRIVATE_UNKNOWN_CODE"}},
            ]
        }
    )
    safe_headers = {
        **quota_headers(),
        "retry-after": "17",
        "set-cookie": "PRIVATE_COOKIE",
        "x-ratelimit-endpoint-name": "PRIVATE_ENDPOINT",
    }

    class Reply(io.BytesIO):
        def __init__(self):
            super().__init__(json.dumps(body).encode())
            self.code, self.headers = status, safe_headers

    class Opener:
        def open(self, request, timeout):
            # Same real audit gateway event, supplied by the socket-free opener.
            sys.audit(
                "urllib.Request",
                request.full_url,
                request.data,
                {"Authorization": "PRIVATE_CREDENTIAL"},
                request.get_method(),
            )
            return Reply()

    monkeypatch.setattr(transport_module, "build_opener", lambda *args: Opener())
    store = observed_quota(tmp_path / "quota", EXPECTED.viewer_id)
    client = LinearClient(
        HttpTransport(_quota_store=store), lambda: Credentials("fixture-secret"), EXPECTED
    )
    try:
        query = ("query" if status == 200 else "mutation") + " PrivateProbe { viewer { id } }"
        variables = {"secret": "PRIVATE_VARIABLE"}
        intents = []
        if category is None:
            assert client.request(query, variables) == {"ok": True}
        else:
            with pytest.raises(LinearError) as caught:
                client.request(
                    query,
                    variables,
                    mutation=True,
                    _before_dispatch=lambda: intents.append("current-effect-fence"),
                )
            assert caught.value.category == category and caught.value.uncertain
            assert intents == ["current-effect-fence"]
            if status == 429:
                assert caught.value.retry_after == 17
        observed = read_private(receipt)
        assert len(observed["dispatches"]) == 1 and len(observed["http"]) == 1
        event = observed["http"][0]
        assert event["status"] == status and event["dispatch_index"] == 1
        assert event["query_sha256"] == observed["dispatches"][0]["query_sha256"]
        assert event["variables_sha256"] == observed["dispatches"][0]["variables_sha256"]
        assert event["retry_after"] == "17" and event["quota_headers"]["x-complexity"] == "2"
        assert event["elapsed_seconds"] >= 0 and event["finished_at"] >= event["started_at"]
        assert any(frame["function"] == "request" for frame in event["caller"])
        assert event["error_codes"] == ([] if status == 200 else [code])
        assert observer.record.get("observation_errors", 0) == 0
        assert sys.monitoring.get_events(observer.tool) == 0
        assert sys.monitoring.get_local_events(observer.tool, observer.code) == (
            sys.monitoring.events.PY_RETURN
        )
        raw = receipt.read_text()
        for secret in ("PRIVATE_", "fixture-secret", query, "set-cookie"):
            assert secret not in raw
    finally:
        observer.close()
        assert sys.monitoring.get_tool(observer.tool) is None
        assert read_private(receipt)["http_monitoring_closed"] is True


def test_concurrent_audit_returns_retain_each_threads_exact_request(tmp_path):
    import hashlib
    import threading

    from ai_task_manager.adapters.linear.models import WireResponse

    observer = DispatchObserver(ledger(tmp_path), 'https://api.linear.app/graphql',
                                tmp_path / 'overlap.json', {})
    original_lock = observer.lock
    a_waiting, b_finished = threading.Event(), threading.Event()
    errors = []

    class OrderedLock:
        delayed = False

        def __enter__(self):
            if threading.current_thread().name == 'audit-A' and not self.delayed:
                self.delayed = True
                a_waiting.set()
                assert b_finished.wait(2), 'Owned B observer did not complete'
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

    observer.lock = OrderedLock()
    packets = {
        'A': {'query': 'query A { viewer { id } }', 'variables': {'id': 'A'}},
        'B': {'query': 'query B { viewer { id } }', 'variables': {'id': 'B'}},
    }

    def worker(name, status):
        try:
            observer.audit('urllib.Request', (
                observer.endpoint, json.dumps(packets[name]).encode(), {}, 'POST'
            ))
            observer._returned(observer.code, 0, WireResponse(status, {}, b'{}'))
        except BaseException as error:
            errors.append(error)
        finally:
            if name == 'B':
                b_finished.set()

    first = threading.Thread(target=worker, args=('A', 500), name='audit-A')
    second = threading.Thread(target=worker, args=('B', 429), name='audit-B')
    try:
        first.start()
        assert a_waiting.wait(2), 'Owned A audit did not reach the overlap'
        second.start()
        first.join(2)
        second.join(2)
        assert not errors and not first.is_alive() and not second.is_alive()
        observed = read_private(observer.receipt)
        assert len(observed['dispatches']) == len(observed['http']) == 2
        for name, status in [('A', 500), ('B', 429)]:
            event = next(row for row in observed['http'] if row['status'] == status)
            packet = packets[name]
            assert event['query_sha256'] == hashlib.sha256(packet['query'].encode()).hexdigest()
            canonical = json.dumps(packet['variables'], sort_keys=True,
                                   separators=(',', ':'), ensure_ascii=False).encode()
            assert event['variables_sha256'] == hashlib.sha256(canonical).hexdigest()
            dispatch = observed['dispatches'][event['dispatch_index'] - 1]
            assert dispatch['query_sha256'] == event['query_sha256']
            assert dispatch['variables_sha256'] == event['variables_sha256']
    finally:
        b_finished.set()
        if first.ident is not None:
            first.join(2)
        if second.ident is not None:
            second.join(2)
        observer.close()


def test_dispatch_observer_leaves_high_only_monitor_slots_available(tmp_path):
    import sys

    observer = DispatchObserver(
        ledger(tmp_path), 'https://api.linear.app/graphql', tmp_path / 'allocation.json', {}
    )
    high_owners = []
    try:
        for name in ('return-observer', 'collector-observer', 'receipt-observer'):
            tool = next(i for i in (5, 4, 3) if sys.monitoring.get_tool(i) is None)
            sys.monitoring.use_tool_id(tool, name)
            high_owners.append(tool)
        assert observer.tool in (0, 1, 2) and set(high_owners) == {3, 4, 5}
        assert all(sys.monitoring.get_events(tool) == 0 for tool in [observer.tool, *high_owners])
        assert sys.monitoring.get_local_events(observer.tool, observer.code) == (
            sys.monitoring.events.PY_RETURN
        )
    finally:
        observer.close()
        assert sys.monitoring.get_tool(observer.tool) is None
        for tool in high_owners:
            sys.monitoring.free_tool_id(tool)
            assert sys.monitoring.get_tool(tool) is None
