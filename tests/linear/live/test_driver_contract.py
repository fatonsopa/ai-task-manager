"""Frozen authored controls; no live credential, account, provider or model evidence."""

import hashlib
import hmac
import http.client
import json
import math
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.models import HttpResponse
from tests.linear.helpers import EXPECTED, STATES, WEBHOOK, LinearRig, ProviderFixture

from .fixture_control import FixtureControl, Observation
from .ledger import DriverRefused, Limits, RunLedger, Scope
from .receiver import BoundedReceiver
from .run_primary import BudgetedTransport, DrainWorker, PrimaryRun


def ledger(tmp_path, *, requests=150, projects=1, clock=time.time, deadline=None):
    return RunLedger(
        tmp_path / "driver.sqlite",
        str(uuid4()),
        Scope(
            EXPECTED.workspace_id,
            EXPECTED.viewer_id,
            EXPECTED.team_id,
            EXPECTED.project_id,
            str(uuid4()),
        ),
        Limits(deadline or clock() + 120, requests=requests, projects=projects),
        clock=clock,
    )


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    base = tmp_path / "provider"
    base.mkdir(mode=0o700)
    try:
        yield LinearRig(base, provider)
    finally:
        provider.close()


def send(
    server, headers=(), body=b"", *, route=None, method="POST", length=None, omit_length=False
):
    connection = http.client.HTTPConnection(*server.address, timeout=6)
    try:
        connection.putrequest(method, route or server.route)
        for key, value in headers:
            connection.putheader(key, value)
        if not omit_length and not any(key.lower() == "content-length" for key, _ in headers):
            connection.putheader("Content-Length", str(len(body)) if length is None else length)
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def wait_for(check, seconds=3):
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, "Owned condition did not arrive within its bound"
        time.sleep(0.01)


def facts(rig):
    with rig.db.transaction() as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "outbox", "attempts", "resource_leases", "inbox", "events")
        }


def test_ld01_ld03_actual_receiver_preserves_raw_bytes_and_deduplicates(rig, tmp_path):
    task = rig.task()
    headers, raw = rig.signed(rig.provider.event(task.issue_id))
    raw = b"  " + raw + b"\n"
    signature = hmac.new(b"fixture-webhook-signing-secret", raw, hashlib.sha256).hexdigest()
    headers = [(key, signature if key == "Linear-Signature" else value) for key, value in headers]
    server = BoundedReceiver(rig.receiver, ledger(tmp_path))
    try:
        server.start()
        assert send(server, headers, raw)[0] == 200
        assert send(server, headers, raw)[0] == 200
        deliveries = rig.journal.deliveries(rig.scope)
        assert (
            len(deliveries) == 1 and deliveries[0]["raw_digest"] == hashlib.sha256(raw).hexdigest()
        )
        assert len(facts(rig)["inbox"]) == 1
        assert rig.current(task) == task  # Receipt persistence is not AI/worker/status settlement.
    finally:
        assert server.stop()["stopped"]


@pytest.mark.parametrize(
    "change,expected",
    [
        ("wrong_route", 404),
        ("method", 405),
        ("missing_length", 400),
        ("invalid_length", 400),
        ("duplicate_length", 400),
        ("chunked", 400),
        ("negative_length", 400),
        ("duplicate_event", 400),
        ("modified_body", 401),
        ("bad_signature", 401),
        ("oversize", 413),
    ],
)
def test_ld02_ld03_omissions_framing_and_changed_signature_have_no_product_effect(
    rig, tmp_path, change, expected
):
    task = rig.task()
    headers, raw = rig.signed(rig.provider.event(task.issue_id))
    options = {}
    maximum = 1048576
    if change == "wrong_route":
        options["route"] = "/not-the-owned-route"
    elif change == "method":
        options["method"] = "PUT"
    elif change == "missing_length":
        options["omit_length"] = True
    elif change == "invalid_length":
        options["length"] = "invalid"
    elif change == "negative_length":
        options["length"] = "-1"
    elif change == "duplicate_length":
        headers += [("Content-Length", str(len(raw))), ("Content-Length", str(len(raw)))]
    elif change == "chunked":
        headers += [("Transfer-Encoding", "chunked")]
    elif change == "duplicate_event":
        headers += [("Linear-Event", "Issue")]
    elif change == "modified_body":
        raw += b" "
    elif change == "bad_signature":
        headers = [
            (key, "0" * 64 if key == "Linear-Signature" else value) for key, value in headers
        ]
    else:
        maximum = 16
    before = facts(rig)
    server = BoundedReceiver(rig.receiver, ledger(tmp_path), maximum=maximum)
    try:
        server.start()
        assert send(server, headers, raw, **options)[0] == expected
        assert facts(rig) == before and not rig.journal.deliveries(rig.scope)
    finally:
        assert server.stop()["stopped"]


def test_ld04_authenticated_unregistered_content_is_never_persisted_or_echoed(rig, tmp_path):
    task = rig.task()
    event = rig.provider.event(task.issue_id)
    sentinel = "PRIVATE_UNREGISTERED_SENTINEL"
    event["data"].update(id=str(uuid4()), title=sentinel, description=sentinel)
    headers, raw = rig.signed(event)
    record = ledger(tmp_path)
    server, before = BoundedReceiver(rig.receiver, record), facts(rig)
    try:
        server.start()
        status, response = send(server, headers, raw)
        assert status == 200 and sentinel.encode() not in response
        assert facts(rig) == before and not rig.journal.deliveries(rig.scope)
        ignored = rig.journal.ignored(rig.scope)
        assert len(ignored) == 1 and ignored[0]["raw_digest"] == hashlib.sha256(raw).hexdigest()
    finally:
        assert server.stop()["stopped"]
    for path in [record.path, Path(rig.journal.path), Path(rig.db.path)]:
        assert sentinel.encode() not in path.read_bytes()


def test_ld05_unexpected_exception_has_fixed_response_and_no_exception_text(tmp_path):
    class Fault:
        def receive(self, *args):
            raise RuntimeError("PRIVATE_EXCEPTION_SENTINEL")

    record = ledger(tmp_path)
    server = BoundedReceiver(Fault(), record)
    try:
        server.start()
        status, body = send(server, body=b"{}")
        assert status == 500 and b"PRIVATE" not in body
    finally:
        assert server.stop()["stopped"]
    assert b"PRIVATE_EXCEPTION_SENTINEL" not in record.path.read_bytes()
    assert any(row["kind"] == "adapter_failed" for row in record.summary()["observations"])


@pytest.mark.parametrize("release_after,underlying_status", [(6.0, 200), (11.2, 503)])
def test_ld06_ld07_ld08_real_sqlite_contention_outlives_http_wait_and_retains_capacity(
    rig, tmp_path, release_after, underlying_status
):
    task = rig.task()
    headers, raw = rig.signed(rig.provider.event(task.issue_id))
    record = ledger(tmp_path)
    server = BoundedReceiver(rig.receiver, record, capacity=1, http_wait=4.0)
    lock = rig.db.connect()
    assert lock.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        server.start()
        assert send(server, headers, raw)[0] == 503
        elapsed = time.monotonic() - started
        assert 3.8 <= elapsed < 5.5 and server.active == 1
        assert send(server, headers, raw)[0] == 503  # No replacement worker while original lives.
        short = server.stop(0.05)
        assert not short["stopped"] and short["active_requests"] == 1
        time.sleep(max(0, release_after - (time.monotonic() - started)))
    finally:
        lock.rollback()
        lock.close()
        stopped = server.stop(12)
    assert stopped["stopped"] and stopped["active_requests"] == 0
    rows = [
        json.loads(row["payload"])
        for row in record.summary()["observations"]
        if row["kind"] == "adapter_finished"
    ]
    assert len(rows) == 1 and rows[0]["underlying_status"] == underlying_status and rows[0]["late"]
    assert len(rig.journal.deliveries(rig.scope)) == (1 if underlying_status == 200 else 0)
    assert len(facts(rig)["inbox"]) == (1 if underlying_status == 200 else 0)
    assert rows[0]["seconds"] >= (5.5 if underlying_status == 200 else 9.5)


class MemoryProvider:
    """Authored local port: this is not Linear or a trusted live authority."""

    def __init__(self):
        self.rows, self.calls = {}, []
        self.lose_reply = False

    def create(self, kind, candidate, scope, marker, content_digest):
        self.calls.append(("create", candidate))
        row = Observation(
            kind, candidate, scope.workspace, scope.team, scope.project, scope.owner, marker
        )
        self.rows[candidate] = row
        if self.lose_reply:
            raise RuntimeError("PRIVATE_PROVIDER_EXCEPTION")
        return row

    def read(self, kind, resource_id):
        self.calls.append(("read", resource_id))
        return self.rows.get(resource_id)

    def cleanup(self, kind, resource_id, action):
        self.calls.append((action, resource_id))
        state = {
            "archive": "archived",
            "cancel": "canceled",
            "disable": "disabled",
            "delete": "deleted",
        }[action]
        self.rows[resource_id] = replace(self.rows[resource_id], state=state)
        return self.rows[resource_id]


def create(control, kind="project", key="create-one"):
    candidate = control.ledger.scope.project if kind == "project" else str(uuid4())
    return control.create(key, kind, candidate, "a" * 64)


def test_ld09_confirmation_send_recovery_cleanup_cannot_skip_intent(tmp_path):
    record = ledger(tmp_path)
    provider = MemoryProvider()
    control = FixtureControl(record, provider)
    for action in [
        lambda: record.mark_sent("missing"),
        lambda: record.confirm("missing", record.scope.project, "project"),
        lambda: control.recover("missing"),
        lambda: control.cleanup(record.scope.project, "archive"),
    ]:
        with pytest.raises(DriverRefused):
            action()
    assert not provider.calls and not record.summary()["resources"]


def test_ld10_exact_committed_replay_restart_and_collision(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    result = create(control)
    reloaded = RunLedger(record.path, record.run_id, record.scope, record.limits)
    assert create(FixtureControl(reloaded, provider)) == result
    assert len(provider.calls) == 1 and len(reloaded.summary()["resources"]) == 1
    with pytest.raises(DriverRefused, match="collision"):
        control.create("create-one", "project", record.scope.project, "b" * 64)
    assert len(provider.calls) == 1


def test_ld11_lost_reply_and_absent_readback_never_recreate_or_free_slot(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    provider.lose_reply = True
    control = FixtureControl(record, provider)
    with pytest.raises(DriverRefused, match="unconfirmed"):
        create(control)
    assert record.get("create-one")["state"] == "uncertain"
    with pytest.raises(DriverRefused, match="readback"):
        create(control)
    with pytest.raises(DriverRefused, match="resource_budget"):
        create(control, key="create-again")
    saved = provider.rows.pop(record.scope.project)
    with pytest.raises(DriverRefused, match="recovery_unconfirmed"):
        control.recover("create-one")
    provider.rows[record.scope.project] = saved
    assert control.recover("create-one")["id"] == record.scope.project
    assert sum(call[0] == "create" for call in provider.calls) == 1
    assert b"PRIVATE_PROVIDER_EXCEPTION" not in record.path.read_bytes()


@pytest.mark.parametrize("field", ["id", "owner", "project", "marker", "team"])
def test_ld12_foreign_current_readback_prevents_cleanup_before_mutation(tmp_path, field):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    create(control)
    provider.rows[record.scope.project] = replace(
        provider.rows[record.scope.project], **{field: str(uuid4())}
    )
    with pytest.raises(DriverRefused, match="cleanup_unconfirmed"):
        control.cleanup(record.scope.project, "archive")
    assert not any(call[0] == "archive" for call in provider.calls)
    assert record.resource(record.scope.project)["state"] == "active"


def test_ld12_names_do_not_authorize_cleanup_and_children_must_close(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    issue = create(control, "issue", "owned-issue")
    hook = create(control, "webhook", "owned-hook")
    comment = create(control, "comment", "owned-comment")
    for candidate, action in [
        (str(uuid4()), "archive"),
        (issue["id"], "delete"),
        (comment["id"], "delete"),
        (project["id"], "archive"),
    ]:
        before = list(provider.calls)
        with pytest.raises(DriverRefused):
            control.cleanup(candidate, action)
        assert provider.calls == before
    control.cleanup(issue["id"], "cancel")
    control.cleanup(hook["id"], "disable")
    assert control.cleanup(project["id"], "archive")["state"] == "archived"
    assert record.resource(comment["id"])["state"] == "active"  # Retained, not deleted.


def test_ld13_actual_threads_share_exact_request_budget(tmp_path):
    record = ledger(tmp_path, requests=3)

    def charge(_):
        try:
            record.request("product_transport")
            return "charged"
        except DriverRefused:
            return "refused"

    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(charge, range(12)))
    assert result.count("charged") == len(record.summary()["requests"]) == 3


def charge_process(path, run, scope, limits, barrier, queue):
    record = RunLedger(path, run, Scope(**scope), Limits(**limits))
    barrier.wait(timeout=5)
    result = []
    for _ in range(4):
        try:
            record.request("product_transport")
            result.append("charged")
        except DriverRefused:
            result.append("refused")
    queue.put(result)


def test_ld13_actual_processes_do_not_reset_or_overspend_shared_budget(tmp_path):
    record = ledger(tmp_path, requests=3)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=charge_process,
            args=(
                record.path,
                record.run_id,
                asdict(record.scope),
                asdict(record.limits),
                barrier,
                queue,
            ),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        result = [item for _ in processes for item in queue.get(timeout=10)]
        for process in processes:
            process.join(timeout=5)
        assert all(process.exitcode == 0 for process in processes)
        assert result.count("charged") == len(record.summary()["requests"]) == 3
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        queue.close()
        queue.join_thread()


def test_ld10_concurrent_same_operation_cannot_duplicate_create(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    control, barrier = FixtureControl(record, provider), threading.Barrier(2)

    def call(_):
        barrier.wait(timeout=3)
        try:
            return create(control)
        except DriverRefused:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(call, range(2)))
    assert any(result) and sum(row[0] == "create" for row in provider.calls) == 1
    assert len(record.summary()["resources"]) == 1


@pytest.mark.parametrize("change", ["run", "scope", "limits"])
def test_ld10_restart_cannot_replace_the_frozen_binding(tmp_path, change):
    record = ledger(tmp_path)
    run, scope, limits = record.run_id, record.scope, record.limits
    if change == "run":
        run = str(uuid4())
    elif change == "scope":
        scope = replace(scope, owner=str(uuid4()))
    else:
        limits = replace(limits, requests=100)
    before = record.summary()
    with pytest.raises(DriverRefused, match="binding_changed"):
        RunLedger(record.path, run, scope, limits)
    assert record.summary() == before


def test_ld04_diagnostic_fields_and_codes_cannot_receive_raw_content(tmp_path):
    record = ledger(tmp_path)
    for kind, fields in [
        ("adapter_failed", {"body": "PRIVATE_CONTENT"}),
        ("adapter_failed", {"code": "private_foreign_payload"}),
        ("private_foreign_payload", {}),
        ("adapter_finished", {"seconds": math.nan}),
    ]:
        with pytest.raises(DriverRefused):
            record.observe(kind, **fields)
    assert not record.summary()["observations"]
    assert b"private_foreign_payload" not in record.path.read_bytes()


def test_ld13_transport_budget_and_expiry_refuse_before_delegation(tmp_path):
    now = [1000.0]
    record = ledger(tmp_path, requests=2, clock=lambda: now[0], deadline=1100)

    class Port:
        def __init__(self):
            self.calls = 0

        def send(self, *args):
            self.calls += 1
            return object()

    port = Port()
    counted = BudgetedTransport(record, port)
    counted.send("fixture", {}, None)
    now[0] = 1100
    with pytest.raises(DriverRefused, match="deadline"):
        counted.send("fixture", {}, None)
    now[0] = 1001
    counted.send("fixture", {}, None)
    with pytest.raises(DriverRefused, match="request_budget"):
        counted.send("fixture", {}, None)
    assert port.calls == 2 and len(record.summary()["requests"]) == 2


@pytest.mark.parametrize(
    "limits", [Limits(math.nan), Limits(-1), Limits(1000, requests=0), Limits(1000, issues=True)]
)
def test_ld13_invalid_limits_cannot_open_an_effect_budget(limits):
    with pytest.raises(DriverRefused):
        limits.check()


def test_ld14_actual_receiver_wakes_real_drain_on_separate_thread(rig, tmp_path):
    task = rig.task()
    rig.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    seen = []

    class TracedInbound:
        def run_once(self, handle):
            seen.append(threading.current_thread().name)
            return rig.inbound.run_once(handle)

    run = PrimaryRun(
        rig.core,
        rig.handle,
        rig.journal,
        rig.client.binding,
        WEBHOOK,
        lambda: b"fixture-webhook-signing-secret",
        TracedInbound(),
        ledger(tmp_path),
        clock=lambda: rig.now,
    )
    try:
        run.start()
        headers, raw = rig.signed(rig.provider.event(task.issue_id))
        assert send(run.receiver, headers, raw)[0] == 200
        wait_for(lambda: rig.current(task).observed_status == "Todo")
        assert seen and set(seen) == {"owned-linear-drain"}
    finally:
        assert run.stop()["stopped"]


def test_ld14_finite_wake_and_truthful_blocked_drain_stop(tmp_path):
    record = ledger(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Inbound:
        def run_once(self, handle):
            calls.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return {"state": "applied"}

    worker = DrainWorker(Inbound(), "authored-handle", record, maximum_per_wake=3)
    try:
        worker.start()
        assert not entered.is_set()
        worker.wake()
        assert entered.wait(1)
        assert not worker.stop(0.01)["stopped"]
        release.set()
    finally:
        assert worker.stop(3)["stopped"]
    assert len(calls) == 1


def test_ld14_wake_drains_at_most_its_frozen_bound_without_polling(tmp_path):
    record, calls = ledger(tmp_path), []

    class Inbound:
        def run_once(self, handle):
            calls.append(threading.get_ident())
            return {"state": "applied"}

    worker = DrainWorker(Inbound(), "authored-handle", record, maximum_per_wake=3)
    try:
        worker.start()
        worker.wake()
        wait_for(
            lambda: any(
                row["kind"] == "drain_wake_completed" for row in record.summary()["observations"]
            )
        )
        assert len(calls) == 3
        time.sleep(0.05)
        assert len(calls) == 3
    finally:
        assert worker.stop()["stopped"]


def test_ld13_receiver_request_budget_refuses_before_another_adapter(tmp_path):
    class Adapter:
        def __init__(self):
            self.calls = 0

        def receive(self, *args):
            self.calls += 1
            return HttpResponse(200)

    adapter, record = Adapter(), ledger(tmp_path, requests=1)
    server = BoundedReceiver(adapter, record)
    try:
        server.start()
        assert send(server, body=b"{}")[0] == 200
        assert send(server, body=b"{}")[0] == 503
        assert adapter.calls == 1
    finally:
        assert server.stop()["stopped"]


@pytest.mark.parametrize(
    "options", [{"host": "0.0.0.0"}, {"capacity": 5}, {"http_wait": math.inf}, {"maximum": 0}]
)
def test_ld13_invalid_receiver_limits_refuse_before_bind(tmp_path, options):
    with pytest.raises(DriverRefused):
        BoundedReceiver(None, ledger(tmp_path), **options)


def test_ld15_default_provider_does_not_fall_back_to_live(tmp_path):
    record = ledger(tmp_path)
    control = FixtureControl(record)
    with pytest.raises(DriverRefused, match="provider_not_admitted"):
        create(control)
    assert all(not value for value in record.summary().values())
    for module in ["fixture_control.py", "run_primary.py"]:
        source = Path(__file__).with_name(module).read_text()
        assert "read_credentials" not in source and "subprocess" not in source
