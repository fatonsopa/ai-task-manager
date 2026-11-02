"""Frozen authored controls; no live credential, account, provider or model evidence."""

import hashlib
import hmac
import json
import math
import multiprocessing
import os
import socket
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.models import HttpResponse
from tests.linear.helpers import EXPECTED, STATES, WEBHOOK, LinearRig, ProviderFixture

from . import receiver as receiver_module
from .client import send_request
from .fixture_control import FixtureControl, Observation
from .http_observation import ReceiverTrace, clock_rows
from .ledger import DriverRefused, Limits, RunLedger, Scope
from .receiver import BoundedReceiver
from .run_primary import BudgetedTransport, DrainWorker, PrimaryRun


def ledger(tmp_path, *, requests=118, projects=1, clock=time.time, deadline=None):
    # Version2 fixtures retain each old ordinary allowance and assertions. Add
    # an explicit reserved partition in a NEW fixture, never reset an old run.
    cleanup = 32 if requests == 118 else 1
    started = clock()
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
        Limits(
            (deadline or started + 120) + 120,
            requests=requests + cleanup,
            projects=projects,
            started_at=started,
            cleanup_requests=cleanup,
            cleanup_seconds=120,
        ),
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
    return send_request(
        server, headers, body, route=route, method=method, length=length, omit_length=omit_length
    )


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


def test_ld16_different_key_cannot_reuse_reserved_candidate(tmp_path):
    record, provider = ledger(tmp_path, projects=2), MemoryProvider()
    control = FixtureControl(record, provider)
    create(control)
    before = record.summary(), list(provider.calls)
    with pytest.raises(DriverRefused, match="candidate_already_reserved"):
        create(control, key="alias")
    assert (record.summary(), provider.calls) == before


def test_ld17_actual_threads_different_keys_have_one_create(tmp_path):
    record, provider = ledger(tmp_path, projects=2), MemoryProvider()
    control, barrier = FixtureControl(record, provider), threading.Barrier(2)

    def call(number):
        barrier.wait(timeout=3)
        try:
            return create(control, key=f"different-{number}")
        except DriverRefused as error:
            assert str(error) == "candidate_already_reserved"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(call, range(2)))
    assert sum(bool(row) for row in results) == 1
    assert len(provider.calls) == len(record.summary()["operations"]) == 1


class SQLiteProvider:
    """Authored cross-process primitive recorder; never a real Linear transport."""

    def __init__(self, path, *, crash_cleanup=False):
        self.path, self.crash_cleanup = str(path), crash_cleanup
        with sqlite3.connect(self.path, timeout=2) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rows(id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS calls(kind TEXT NOT NULL,id TEXT NOT NULL)")

    def create(self, kind, candidate, scope, marker, content_digest):
        row = Observation(
            kind, candidate, scope.workspace, scope.team, scope.project, scope.owner, marker
        )
        with sqlite3.connect(self.path, timeout=2) as conn:
            conn.execute("INSERT INTO calls VALUES('create',?)", (candidate,))
            conn.execute("INSERT INTO rows VALUES(?,?)", (candidate, json.dumps(asdict(row))))
        return row

    def read(self, kind, candidate):
        with sqlite3.connect(self.path, timeout=2) as conn:
            conn.execute("INSERT INTO calls VALUES('read',?)", (candidate,))
            row = conn.execute("SELECT payload FROM rows WHERE id=?", (candidate,)).fetchone()
        return Observation(**json.loads(row[0])) if row else None

    def cleanup(self, kind, candidate, action):
        state = {
            "archive": "archived",
            "cancel": "canceled",
            "disable": "disabled",
            "delete": "deleted",
        }[action]
        with sqlite3.connect(self.path, timeout=2) as conn:
            old = conn.execute("SELECT payload FROM rows WHERE id=?", (candidate,)).fetchone()
            row = replace(Observation(**json.loads(old[0])), state=state)
            conn.execute("INSERT INTO calls VALUES(?,?)", (action, candidate))
            conn.execute(
                "UPDATE rows SET payload=? WHERE id=?", (json.dumps(asdict(row)), candidate)
            )
        if self.crash_cleanup:
            os._exit(17)  # Actual owned child crash after external fixture commit.
        return row

    def count(self, kind):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM calls WHERE kind=?", (kind,)).fetchone()[0]


def alias_process(path, run, scope, limits, provider_path, barrier, queue, number):
    record = RunLedger(path, run, Scope(**scope), Limits(**limits))
    control = FixtureControl(record, SQLiteProvider(provider_path))
    barrier.wait(timeout=5)
    try:
        create(control, key=f"process-{number}")
        queue.put("created")
    except DriverRefused as error:
        queue.put(str(error))


def join_owned(processes):
    for process in processes:
        if process.pid is None:
            continue
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        assert not process.is_alive()


def test_ld18_actual_processes_different_keys_have_one_primitive(tmp_path):
    record = ledger(tmp_path, projects=2)
    provider = SQLiteProvider(tmp_path / "authored-provider.sqlite")
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=alias_process,
            args=(
                record.path,
                record.run_id,
                asdict(record.scope),
                asdict(record.limits),
                provider.path,
                barrier,
                queue,
                i,
            ),
        )
        for i in range(2)
    ]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=5)
        assert all(process.exitcode == 0 for process in processes)
        assert sorted(results) == ["candidate_already_reserved", "created"]
        assert provider.count("create") == len(record.summary()["operations"]) == 1
    finally:
        join_owned(processes)
        queue.close()
        queue.join_thread()


def test_ld19_restart_uncertainty_cannot_be_aliased(tmp_path):
    record, provider = ledger(tmp_path, projects=2), MemoryProvider()
    provider.lose_reply = True
    control = FixtureControl(record, provider)
    with pytest.raises(DriverRefused, match="unconfirmed"):
        create(control)
    reloaded = RunLedger(record.path, record.run_id, record.scope, record.limits)
    recovered = FixtureControl(reloaded, provider)
    with pytest.raises(DriverRefused, match="candidate_already_reserved"):
        create(recovered, key="replacement")
    assert recovered.recover("create-one")["id"] == record.scope.project
    assert sum(call[0] == "create" for call in provider.calls) == 1


def test_ld20_candidate_cannot_move_to_another_kind(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    create(control)
    with pytest.raises(DriverRefused, match="candidate_already_reserved"):
        control.create("cross-kind", "issue", record.scope.project, "b" * 64)
    assert len(provider.calls) == 1 and len(record.summary()["operations"]) == 1


def test_ld21_legacy_duplicate_reservations_refuse_without_rewriting_rows(tmp_path):
    record = ledger(tmp_path)
    record.begin("first", "project", record.scope.project, "a" * 64)
    with sqlite3.connect(record.path) as conn:
        conn.execute("ALTER TABLE operations RENAME TO operations_old")
        conn.execute(
            "CREATE TABLE operations(key TEXT PRIMARY KEY,fingerprint TEXT,"
            "kind TEXT,candidate TEXT,state TEXT)"
        )
        conn.execute("INSERT INTO operations SELECT * FROM operations_old")
        conn.execute(
            "INSERT INTO operations SELECT 'alias',fingerprint,kind,candidate,state "
            "FROM operations_old"
        )
        conn.execute("DROP TABLE operations_old")
        conn.execute("DROP TABLE driver_schema")
    before = record.path.read_bytes()
    with pytest.raises(DriverRefused, match="legacy_duplicate_reservations"):
        RunLedger(record.path, record.run_id, record.scope, record.limits)
    assert record.path.read_bytes() == before


def test_ld22_cleanup_omissions_and_wrong_order_cannot_delegate(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    before = record.summary(), list(provider.calls)
    for action in [
        lambda: record.cleanup_read("missing"),
        lambda: record.cleanup_prepared("missing", "a" * 64),
        lambda: record.cleanup_sent("missing"),
        lambda: record.cleanup_confirm("missing", "a" * 64),
        lambda: record.cleaned(project["id"], "archived"),
        lambda: control.cleanup(str(uuid4()), "archive"),
    ]:
        with pytest.raises(DriverRefused):
            action()
    assert (record.summary(), provider.calls) == before
    record.begin_cleanup("action", project["id"], "archive")
    with pytest.raises(DriverRefused, match="read_intent_missing"):
        record.cleanup_prepared("action", "a" * 64)
    with pytest.raises(DriverRefused, match="not_sendable"):
        record.cleanup_sent("action")
    assert provider.calls == before[1]


def test_ld23_cleanup_callback_observes_exact_durable_sent_action(tmp_path):
    record = ledger(tmp_path)

    class InspectedProvider(MemoryProvider):
        def cleanup(self, kind, candidate, action):
            with sqlite3.connect(record.path) as conn:
                conn.row_factory = sqlite3.Row
                row = dict(conn.execute("SELECT * FROM cleanup_actions").fetchone())
                binding = json.loads(conn.execute("SELECT value FROM meta").fetchone()[0])
                requests = conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE cleanup_key=? "
                    "AND purpose='cleanup_effect'",
                    (row["key"],),
                ).fetchone()[0]
            assert (
                row["resource_id"],
                row["kind"],
                row["action"],
                row["target"],
                row["state"],
                row["attempts"],
            ) == (candidate, kind, action, "archived", "sent", 1)
            assert len(row["before_digest"]) == 64 and requests == 1
            assert binding["run"] == record.run_id and binding["scope"] == asdict(record.scope)
            return super().cleanup(kind, candidate, action)

    provider = InspectedProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    assert control.cleanup(project["id"], "archive")["state"] == "archived"
    row = record.summary()["cleanup_actions"][0]
    assert row["state"] == "confirmed" and len(row["result_digest"]) == 64


class LostCleanup(MemoryProvider):
    def __init__(self):
        super().__init__()
        self.lose_cleanup = True

    def cleanup(self, *args):
        result = super().cleanup(*args)
        if self.lose_cleanup:
            self.lose_cleanup = False
            raise RuntimeError("PRIVATE_CLEANUP_EXCEPTION")
        return result


def test_ld24_cleanup_lost_reply_reconciles_without_resend(tmp_path):
    record, provider = ledger(tmp_path), LostCleanup()
    control = FixtureControl(record, provider)
    project = create(control)
    with pytest.raises(DriverRefused, match="cleanup_unconfirmed"):
        control.cleanup(project["id"], "archive", operation_key="cleanup-one")
    assert record.cleanup_action("cleanup-one")["state"] == "uncertain"
    reloaded = RunLedger(record.path, record.run_id, record.scope, record.limits)
    assert (
        FixtureControl(reloaded, provider).cleanup(
            project["id"], "archive", operation_key="cleanup-one"
        )["state"]
        == "archived"
    )
    assert sum(call[0] == "archive" for call in provider.calls) == 1
    assert reloaded.cleanup_action("cleanup-one")["attempts"] == 1
    assert b"PRIVATE_CLEANUP_EXCEPTION" not in record.path.read_bytes()


def cleanup_crash_process(path, run, scope, limits, provider_path, candidate):
    record = RunLedger(path, run, Scope(**scope), Limits(**limits))
    control = FixtureControl(record, SQLiteProvider(provider_path, crash_cleanup=True))
    control.cleanup(candidate, "archive", operation_key="crash-cleanup")


def test_ld25_actual_crash_after_cleanup_effect_is_recovered(tmp_path):
    record = ledger(tmp_path)
    provider = SQLiteProvider(tmp_path / "authored-provider.sqlite")
    control = FixtureControl(record, provider)
    project = create(control)
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=cleanup_crash_process,
        args=(
            record.path,
            record.run_id,
            asdict(record.scope),
            asdict(record.limits),
            provider.path,
            project["id"],
        ),
    )
    try:
        child.start()
        child.join(timeout=10)
        assert child.exitcode == 17 and not child.is_alive()
        assert record.cleanup_action("crash-cleanup")["state"] == "sent"
        assert record.resource(project["id"])["state"] == "active"
        before_requests = len(record.summary()["requests"])
        reloaded = RunLedger(record.path, record.run_id, record.scope, record.limits)
        assert (
            FixtureControl(reloaded, provider).recover_cleanup("crash-cleanup")["state"]
            == "archived"
        )
        assert len(reloaded.summary()["requests"]) == before_requests + 1
        assert provider.count("archive") == 1
    finally:
        join_owned([child])


@pytest.mark.parametrize("change", ["absent", "id", "owner", "project", "team", "marker", "state"])
def test_ld26_cleanup_wrong_current_readback_retains_uncertainty(tmp_path, change):
    record, provider = ledger(tmp_path), LostCleanup()
    control = FixtureControl(record, provider)
    project = create(control)
    with pytest.raises(DriverRefused):
        control.cleanup(project["id"], "archive", operation_key="unknown")
    if change == "absent":
        provider.rows.pop(project["id"])
    else:
        provider.rows[project["id"]] = replace(
            provider.rows[project["id"]],
            **{change: "active" if change == "state" else str(uuid4())},
        )
    with pytest.raises(DriverRefused, match="recovery_unconfirmed"):
        control.recover_cleanup("unknown")
    assert record.cleanup_action("unknown")["state"] == "uncertain"
    assert record.resource(project["id"])["state"] == "active"
    assert sum(call[0] == "archive" for call in provider.calls) == 1


def test_ld27_cleanup_action_collision_and_distinct_disable_delete(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    hook = create(control, "webhook")
    control.cleanup(hook["id"], "disable", operation_key="disable")
    before = list(provider.calls)
    for action in [
        lambda: control.cleanup(hook["id"], "delete", operation_key="disable"),
        lambda: record.begin_cleanup("alias", hook["id"], "disable"),
        lambda: record.begin_cleanup(
            "changed-target", hook["id"], "disable", expected_state="deleted"
        ),
    ]:
        with pytest.raises(DriverRefused):
            action()
    assert provider.calls == before
    assert control.cleanup(hook["id"], "delete", operation_key="delete")["state"] == "deleted"
    assert [(row["action"], row["state"]) for row in record.summary()["cleanup_actions"]] == [
        ("disable", "confirmed"),
        ("delete", "confirmed"),
    ]


def test_ld28_uncertain_child_blocks_parent_archive_until_actual_recovery(tmp_path):
    record, provider = ledger(tmp_path), LostCleanup()
    control = FixtureControl(record, provider)
    project = create(control)
    issue = create(control, "issue", "child")
    with pytest.raises(DriverRefused):
        control.cleanup(issue["id"], "cancel", operation_key="child-cancel")
    before = list(provider.calls)
    with pytest.raises(DriverRefused, match="unresolved_cleanup"):
        control.cleanup(project["id"], "archive")
    assert provider.calls == before
    assert control.recover_cleanup("child-cancel")["state"] == "canceled"
    assert control.cleanup(project["id"], "archive")["state"] == "archived"


def test_ld29_committed_cleanup_retry_after_expiry_has_no_new_effect(tmp_path):
    now = [1000.0]
    record, provider = ledger(tmp_path, clock=lambda: now[0], deadline=1100), MemoryProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    first = control.cleanup(project["id"], "archive")
    before = record.summary(), list(provider.calls)
    provider.rows.clear()
    now[0] = record.limits.deadline + 1
    assert control.cleanup(project["id"], "archive") == first
    assert (record.summary(), provider.calls) == before
    with pytest.raises(DriverRefused):
        control.cleanup(project["id"], "archive", operation_key="fresh-operation")
    assert (record.summary(), provider.calls) == before


class CountedPort:
    def __init__(self):
        self.calls = 0

    def send(self, *args):
        self.calls += 1


def saturate_ordinary(record):
    port = CountedPort()
    transport = BudgetedTransport(record, port)
    used = len([row for row in record.summary()["requests"] if row["cleanup_key"] is None])
    for _ in range(record.limits.requests - record.limits.cleanup_requests - used):
        transport.send("authored", {}, None)
    return transport, port


def test_ld30_ordinary_exhaustion_preserves_cleanup_partition(tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    transport, port = saturate_ordinary(record)
    assert len(record.summary()["requests"]) == 118 and port.calls == 117
    with pytest.raises(DriverRefused, match="request_budget"):
        transport.send("authored", {}, None)
    assert control.cleanup(project["id"], "archive")["state"] == "archived"
    rows = record.summary()["requests"]
    assert len(rows) == 120 and sum(row["cleanup_key"] is not None for row in rows) == 2
    assert record.limits.requests == 150 and record.limits.cleanup_requests == 32


def cleanup_charge_process(path, run, scope, limits, provider_path, key, barrier, queue):
    record = RunLedger(path, run, Scope(**scope), Limits(**limits))
    provider = SQLiteProvider(provider_path)
    barrier.wait(timeout=5)
    count = 0
    for _ in range(24):
        try:
            record.cleanup_read(key)
        except DriverRefused as error:
            assert str(error) == "cleanup_budget_exhausted"
        else:
            provider.read("project", record.scope.project)
            count += 1
    queue.put(count)


def test_ld31_actual_processes_and_threads_keep_cumulative_partition(tmp_path):
    record = ledger(tmp_path)
    provider = SQLiteProvider(tmp_path / "authored-provider.sqlite")
    control = FixtureControl(record, provider)
    project = create(control)
    transport, port = saturate_ordinary(record)
    record.begin_cleanup("reserved", project["id"], "archive")
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    children = [
        context.Process(
            target=cleanup_charge_process,
            args=(
                record.path,
                record.run_id,
                asdict(record.scope),
                asdict(record.limits),
                provider.path,
                "reserved",
                barrier,
                queue,
            ),
        )
        for _ in range(2)
    ]

    def denied_ordinary(_):
        with pytest.raises(DriverRefused, match="cleanup_phase"):
            transport.send("authored", {}, None)

    try:
        for child in children:
            child.start()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(denied_ordinary, range(12)))
        counts = [queue.get(timeout=10) for _ in children]
        for child in children:
            child.join(timeout=5)
        assert all(child.exitcode == 0 for child in children) and sum(counts) == 32
        rows = record.summary()["requests"]
        assert len(rows) == 150 and sum(row["cleanup_key"] is None for row in rows) == 118
        assert port.calls == 117 and provider.count("read") == 32
        assert record.cleanup_action("reserved")["state"] == "intent"
        assert record.resource(project["id"])["state"] == "active"
    finally:
        join_owned(children)
        queue.close()
        queue.join_thread()


def test_ld32_strings_booleans_and_missing_context_cannot_use_cleanup_reserve(tmp_path):
    record = ledger(tmp_path)
    before = record.summary()
    for action in [
        lambda: record.request("cleanup"),
        lambda: record.request("cleanup_read"),
        lambda: record.request(True),
        lambda: record.cleanup_read("caller-made"),
    ]:
        with pytest.raises(DriverRefused):
            action()
    assert record.summary() == before


def test_ld33_ordinary_cutoff_and_hard_deadline_are_distinct(tmp_path):
    now = [1000.0]
    record, provider = ledger(tmp_path, clock=lambda: now[0], deadline=1280), MemoryProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    hook = create(control, "webhook", "hook")
    now[0] = record.limits.deadline - 120
    before = list(provider.calls)
    for action in [
        lambda: record.request("receiver_http"),
        lambda: create(control, "issue", "late"),
    ]:
        with pytest.raises(DriverRefused, match="ordinary_deadline"):
            action()
    assert provider.calls == before
    assert control.cleanup(hook["id"], "disable")["state"] == "disabled"
    now[0] = record.limits.deadline
    before = record.summary(), list(provider.calls)
    with pytest.raises(DriverRefused, match="deadline"):
        control.cleanup(project["id"], "archive")
    assert (record.summary(), provider.calls) == before


@pytest.mark.parametrize("field", ["deadline", "started_at", "cleanup_seconds", "cleanup_requests"])
def test_ld34_restart_cannot_reset_reserve_phase_or_counts(tmp_path, field):
    record, provider = ledger(tmp_path), MemoryProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    record.begin_cleanup("cleanup", project["id"], "archive")
    record.cleanup_read("cleanup")
    reloaded = RunLedger(record.path, record.run_id, record.scope, record.limits)
    assert reloaded.summary() == record.summary()
    with pytest.raises(DriverRefused, match="cleanup_phase"):
        reloaded.request("product_transport")
    before = record.path.read_bytes()
    with pytest.raises(DriverRefused, match="binding_changed"):
        RunLedger(
            record.path,
            record.run_id,
            record.scope,
            replace(record.limits, **{field: getattr(record.limits, field) + 1}),
        )
    assert record.path.read_bytes() == before


def test_ld35_late_ordinary_effect_keeps_charge_and_owned_reservation(tmp_path):
    now = [1000.0]
    record = ledger(tmp_path, clock=lambda: now[0], deadline=1100)
    entered, release = threading.Event(), threading.Event()

    class BlockedProvider(MemoryProvider):
        def create(self, *args):
            entered.set()
            assert release.wait(3)
            return super().create(*args)

    provider = BlockedProvider()
    control = FixtureControl(record, provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(create, control)
        try:
            assert entered.wait(1)
            assert record.get("create-one")["state"] == "sent"
            now[0] = record.limits.deadline + 1
            with pytest.raises(DriverRefused, match="deadline"):
                record.request("product_transport")
            assert len(record.summary()["requests"]) == 1
            assert record.limits.cleanup_requests == 32 and not record.summary()["resources"]
        finally:
            release.set()
        assert future.result(timeout=3)["id"] == record.scope.project
    assert (
        len(record.summary()["requests"]) == 1 and record.get("create-one")["state"] == "confirmed"
    )


def test_ld36_exhausted_cleanup_partition_retains_unfinished_action(tmp_path):
    record, provider = ledger(tmp_path, requests=1), MemoryProvider()
    control = FixtureControl(record, provider)
    project = create(control)
    with pytest.raises(DriverRefused, match="cleanup_unconfirmed"):
        control.cleanup(project["id"], "archive", operation_key="no-effect-budget")
    row = record.cleanup_action("no-effect-budget")
    assert row["state"] == "intent" and row["attempts"] == 0
    assert record.resource(project["id"])["state"] == "active"
    assert not any(call[0] == "archive" for call in provider.calls)
    with pytest.raises(DriverRefused, match="unresolved_cleanup"):
        record.require_closed_children()


def test_ld36_actual_live_drain_blocks_parent_cleanup(rig, tmp_path):
    record, provider = ledger(tmp_path), MemoryProvider()
    entered, release = threading.Event(), threading.Event()

    class Inbound:
        def run_once(self, handle):
            entered.set()
            release.wait(3)
            return None

    runtime = PrimaryRun(
        rig.core,
        rig.handle,
        rig.journal,
        rig.client.binding,
        WEBHOOK,
        lambda: b"fixture-webhook-signing-secret",
        Inbound(),
        record,
        clock=lambda: rig.now,
    )
    control = FixtureControl(record, provider, quiescence=runtime.require_quiescent)
    project = create(control)
    try:
        runtime.drain.start()
        runtime.drain.wake()
        assert entered.wait(1)
        before = list(provider.calls)
        with pytest.raises(DriverRefused, match="owned_runtime_not_quiescent"):
            control.cleanup(project["id"], "archive")
        assert provider.calls == before and not record.summary()["cleanup_actions"]
        assert not runtime.stop(0.01)["stopped"]
        release.set()
    finally:
        release.set()
        assert runtime.stop(3)["stopped"]
    assert control.cleanup(project["id"], "archive")["state"] == "archived"


@pytest.mark.parametrize(
    "changes",
    [
        {"started_at": None},
        {"cleanup_requests": None},
        {"cleanup_seconds": None},
        {"cleanup_requests": 0},
        {"cleanup_requests": -1},
        {"cleanup_requests": 150},
        {"cleanup_requests": True},
        {"cleanup_seconds": math.nan},
        {"cleanup_seconds": -1},
        {"cleanup_seconds": 240},
        {"started_at": math.inf},
    ],
)
def test_ld37_missing_or_invalid_reserve_refuses_before_file_creation(tmp_path, changes):
    limits = Limits(1240, started_at=1000, cleanup_requests=32, cleanup_seconds=120)
    scope = Scope(
        EXPECTED.workspace_id,
        EXPECTED.viewer_id,
        EXPECTED.team_id,
        EXPECTED.project_id,
        str(uuid4()),
    )
    path = tmp_path / "never-created.sqlite"
    with pytest.raises(DriverRefused):
        RunLedger(path, str(uuid4()), scope, replace(limits, **changes))
    assert not path.exists()


class HttpCounter:
    def __init__(self, delay=0):
        self.calls, self.delay = 0, delay

    def receive(self, *args):
        self.calls += 1
        time.sleep(self.delay)
        return HttpResponse(200)


def owned_socket(server):
    raw = socket.create_connection(server.address, timeout=3)
    raw.settimeout(3)
    return raw


def request_bytes(server, body=b"{}", *, headers=()):
    return (
        (
            f"POST {server.route} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(body)}\r\n"
        ).encode()
        + b"".join(f"{key}: {value}\r\n".encode() for key, value in headers)
        + b"\r\n"
        + body
    )


def wire_until_closed(raw, observations=None):
    chunks = []
    state = {"started_at": time.monotonic(), "read_error": None}
    try:
        while data := raw.recv(8192):
            chunks.append(data)
        state["ended_by"] = "eof"
    except ConnectionResetError as error:
        state.update(ended_by="reset", read_error=type(error).__name__, errno=error.errno)
    except OSError as error:
        state.update(ended_by="error", read_error=type(error).__name__, errno=error.errno)
        raise
    finally:
        if observations is not None:
            wire = b"".join(chunks)
            observations.update(
                state,
                finished_at=time.monotonic(),
                wire_size=len(wire),
                wire_hex=wire.hex(),
                wire_sha256=hashlib.sha256(wire).hexdigest(),
            )
    return b"".join(chunks)


def clock_records(server, tmp_path):
    rows = clock_rows(server)
    # Persist actual facts before checking their consistency, including failures.
    (tmp_path / "http-clock-records.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n"
    )
    for row in rows:
        if row["response_completed"]:
            assert (
                row["accepted_at"]
                <= row["response_started_at"]
                <= row["response_completed_at"]
                <= row["hard_deadline"]
            )
        else:
            assert row["response_completed_at"] is None
    return rows


def test_hd01_hd15_actual_signed_ingest_uses_observed_monotonic_clock(rig, tmp_path, monkeypatch):
    # Remove wall-clock access from this module without replacing the real clock.
    monkeypatch.setattr(receiver_module, "time", SimpleNamespace(monotonic=time.monotonic))
    task = rig.task()
    headers, raw = rig.signed(rig.provider.event(task.issue_id))
    server = BoundedReceiver(
        rig.receiver, ledger(tmp_path), response_budget=0.8, output_reserve=0.2
    )
    started = time.monotonic()
    try:
        server.start()
        assert send(server, headers, raw)[0] == 200
        assert len(facts(rig)["inbox"]) == 1
    finally:
        assert server.stop(3)["stopped"]
    rows = clock_records(server, tmp_path)
    assert len(rows) == 1 and started <= rows[0]["accepted_at"]
    assert rows[0]["response_completed"] and not rows[0]["workers_alive"]


@pytest.mark.parametrize("part", ["headers", "body"])
def test_hd02_hd03_frequent_drip_cannot_refresh_absolute_deadline(tmp_path, part, monkeypatch):
    counter, record = HttpCounter(), ledger(tmp_path)
    server = BoundedReceiver(
        counter, record, response_budget=0.8, output_reserve=0.2, read_timeout=0.2
    )
    halt = threading.Event()
    raw = thread = None
    trace = ReceiverTrace(monkeypatch, receiver_module)
    client = {}
    stopped = None
    try:
        server.start()
        raw = owned_socket(server)
        initial = f"POST {server.route} HTTP/1.1\r\nX-Private: ".encode()
        if part == "body":
            initial = f"POST {server.route} HTTP/1.1\r\nContent-Length: 100000\r\n\r\n".encode()
        raw.sendall(initial)

        def drip():
            while not halt.wait(0.04):
                try:
                    raw.sendall(b"a")
                except OSError:
                    return

        thread = threading.Thread(target=drip, name="owned-authored-drip")
        thread.start()
        wire = wire_until_closed(raw, client)
        received = time.monotonic()
        assert wire.startswith(b"HTTP/1.0 503 ") and wire.count(b"HTTP/1.0 ") == 1
        lease = server.server.leases[0]
        assert received - lease.connection.accepted_at <= 0.8
        assert counter.calls == 0
    finally:
        active_error = sys.exc_info()[0]
        trace.save(
            tmp_path / "socket-before-cleanup.json",
            clocks=clock_rows(server),
            client=client,
            counter_calls=counter.calls,
            assertion_error=active_error.__name__ if active_error else None,
        )
        try:
            halt.set()
            if thread is not None:
                thread.join(1)
                assert not thread.is_alive()
            if raw is not None:
                raw.close()
            stopped = server.stop(3)
            assert stopped["stopped"]
        finally:
            trace.save(
                tmp_path / "socket-after-cleanup.json",
                clocks=clock_rows(server),
                client=client,
                stop=stopped,
                counter_calls=counter.calls,
                client_descriptor=raw.fileno() if raw is not None else None,
                drip_alive=thread.is_alive() if thread is not None else None,
            )
            rows = clock_records(server, tmp_path)
    assert rows[0]["response_completed"] and not rows[0]["workers_alive"]


@pytest.mark.parametrize("part", ["headers", "body"])
def test_hd16_handler_shutdown_waits_for_actual_deadline_send(tmp_path, monkeypatch, part):
    counter, record = HttpCounter(), ledger(tmp_path)
    server = BoundedReceiver(
        counter, record, response_budget=0.8, output_reserve=0.2, read_timeout=0.2
    )
    trace = ReceiverTrace(monkeypatch, receiver_module)
    writer_entered, close_finished = threading.Event(), threading.Event()
    shutdown_attempted, reader_waited = threading.Event(), threading.Event()
    original_send = receiver_module.DeadlineConnection.sendall
    original_read = receiver_module.DeadlineReader.readinto
    original_shutdown = receiver_module.Server.shutdown_request

    def paused_send(connection, data):
        if threading.current_thread().name.startswith("owned-deadline-"):
            trace.record("controlled_writer_pause", hard_remaining=connection.hard_remaining)
            writer_entered.set()
            close_finished.wait(0.04)
            trace.record("controlled_writer_release", hard_remaining=connection.hard_remaining)
        return original_send(connection, data)

    def late_read(reader, buffer):
        if 0 < reader.connection.work_remaining < 0.09:
            reader_waited.set()
            trace.record("controlled_late_reader_wait")
            writer_entered.wait(0.12)
        return original_read(reader, buffer)

    def observed_shutdown(owned_server, connection):
        shutdown_attempted.set()
        trace.record(
            "controlled_shutdown_attempt", response_done=connection.lease.response_done.is_set()
        )
        try:
            return original_shutdown(owned_server, connection)
        finally:
            close_finished.set()

    monkeypatch.setattr(receiver_module.DeadlineConnection, "sendall", paused_send)
    monkeypatch.setattr(receiver_module.DeadlineReader, "readinto", late_read)
    monkeypatch.setattr(receiver_module.Server, "shutdown_request", observed_shutdown)
    halt = threading.Event()
    raw = thread = stopped = None
    client = {}
    try:
        server.start()
        raw = owned_socket(server)
        initial = f"POST {server.route} HTTP/1.1\r\nX-Private: ".encode()
        if part == "body":
            initial = f"POST {server.route} HTTP/1.1\r\nContent-Length: 100000\r\n\r\n".encode()
        raw.sendall(initial)

        def drip():
            while not halt.wait(0.04):
                try:
                    raw.sendall(b"a")
                except OSError:
                    return

        thread = threading.Thread(target=drip, name="owned-interleaving-drip")
        thread.start()
        wire = wire_until_closed(raw, client)
        received = time.monotonic()
        assert writer_entered.is_set() and reader_waited.is_set() and shutdown_attempted.is_set()
        assert wire.startswith(b"HTTP/1.0 503 ") and wire.count(b"HTTP/1.0 ") == 1
        assert wire.endswith(b"\r\n\r\n")
        assert received - server.server.leases[0].connection.accepted_at <= 0.8
        assert counter.calls == 0
    finally:
        active_error = sys.exc_info()[0]
        trace.save(
            tmp_path / "interleaving-before-cleanup.json",
            clocks=clock_rows(server),
            client=client,
            writer_entered=writer_entered.is_set(),
            reader_waited=reader_waited.is_set(),
            shutdown_attempted=shutdown_attempted.is_set(),
            close_finished=close_finished.is_set(),
            assertion_error=active_error.__name__ if active_error else None,
        )
        try:
            halt.set()
            if thread is not None:
                thread.join(1)
                assert not thread.is_alive()
            if raw is not None:
                raw.close()
            stopped = server.stop(3)
            assert stopped["stopped"]
        finally:
            trace.save(
                tmp_path / "interleaving-after-cleanup.json",
                clocks=clock_rows(server),
                client=client,
                stop=stopped,
                client_descriptor=raw.fileno() if raw is not None else None,
                drip_alive=thread.is_alive() if thread is not None else None,
            )
            rows = clock_records(server, tmp_path)
    assert rows[0]["response_completed"] and not rows[0]["workers_alive"]


def test_hd04_idle_headers_refuse_without_waiting_for_a_new_read_budget(tmp_path):
    counter = HttpCounter()
    server = BoundedReceiver(
        counter, ledger(tmp_path), response_budget=0.8, output_reserve=0.2, read_timeout=0.15
    )
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        assert wire_until_closed(raw).startswith(b"HTTP/1.0 408 ")
        assert counter.calls == 0
    finally:
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert clock_records(server, tmp_path)[0]["response_completed"]


@pytest.mark.parametrize("part", ["request_line", "headers", "body"])
def test_hd05_half_closed_truncated_input_never_dispatches(tmp_path, part):
    counter, record = HttpCounter(), ledger(tmp_path)
    server = BoundedReceiver(counter, record, response_budget=0.8, output_reserve=0.2)
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        prefix = f"POST {server.route} HTTP/1.1\r\n".encode()
        data = {
            "request_line": b"POST",
            "headers": prefix + b"Content-Length: 3\r\nX-Private: PRIVATE_TRUNCATED_SENTINEL\r\n",
            "body": prefix + b"Content-Length: 100\r\n\r\nPRIVATE_TRUNCATED_SENTINEL",
        }[part]
        raw.sendall(data)
        raw.shutdown(socket.SHUT_WR)
        wire = wire_until_closed(raw)
        assert wire.startswith(b"HTTP/1.0 400 ") and b"PRIVATE_TRUNCATED_SENTINEL" not in wire
        assert counter.calls == 0
    finally:
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert b"PRIVATE_TRUNCATED_SENTINEL" not in record.path.read_bytes()
    clock_records(server, tmp_path)


def test_hd06_default_budget_covers_slow_input_and_actual_late_ingest(rig, tmp_path):
    task = rig.task()
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    entered, release = threading.Event(), threading.Event()

    class Delayed:
        def receive(self, *args):
            entered.set()
            assert release.wait(6)
            return rig.receiver.receive(*args)

    server = BoundedReceiver(Delayed(), ledger(tmp_path), capacity=1)
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        raw.settimeout(6)
        request = request_bytes(server, body, headers=headers)
        split = len(request) - len(body)
        raw.sendall(request[:10])
        time.sleep(0.7)
        raw.sendall(request[10 : split + len(body) // 2])
        time.sleep(0.7)
        raw.sendall(request[split + len(body) // 2 :])
        assert entered.wait(1)
        wire = wire_until_closed(raw)
        received = time.monotonic()
        lease = server.server.leases[0]
        assert wire.startswith(b"HTTP/1.0 503 ") and received - lease.connection.accepted_at <= 4.5
        assert lease.adapter_started_at - lease.connection.accepted_at >= 1.3
        assert server.active == 1 and not facts(rig)["inbox"]
        assert not server.stop(0.01)["stopped"]
        release.set()
    finally:
        release.set()
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert len(facts(rig)["inbox"]) == len(rig.journal.deliveries(rig.scope)) == 1
    assert server.server.leases[0].status == 200
    clock_records(server, tmp_path)


def test_hd07_post_accept_queue_wait_keeps_original_deadline(tmp_path, monkeypatch):
    counter, record = HttpCounter(), ledger(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = receiver_module.Server.process_request

    def delayed(server, *args):
        entered.set()
        release.wait(2)
        return original(server, *args)

    monkeypatch.setattr(receiver_module.Server, "process_request", delayed)
    server = BoundedReceiver(counter, record, response_budget=0.6, output_reserve=0.15, capacity=1)
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        raw.sendall(request_bytes(server))
        assert entered.wait(1)
        assert wire_until_closed(raw).startswith(b"HTTP/1.0 503 ")
        assert time.monotonic() - server.server.leases[0].connection.accepted_at <= 0.6
        assert server.active == 1 and not server.stop(0.01)["stopped"]
    finally:
        release.set()
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert counter.calls == 0 and not record.summary()["requests"]
    clock_records(server, tmp_path)


def test_hd08_actual_admission_lock_does_not_delay_response_or_start_late_adapter(tmp_path):
    counter, record = HttpCounter(), ledger(tmp_path)
    lock = sqlite3.connect(record.path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    server = BoundedReceiver(counter, record, response_budget=0.8, output_reserve=0.2, capacity=1)
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        raw.sendall(request_bytes(server))
        assert wire_until_closed(raw).startswith(b"HTTP/1.0 503 ")
        lease = server.server.leases[0]
        assert time.monotonic() - lease.connection.accepted_at <= 0.8
        assert server.active == 1 and not server.stop(0.01)["stopped"]
        time.sleep(0.3)
    finally:
        lock.rollback()
        lock.close()
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert counter.calls == 0 and len(record.summary()["requests"]) == 1
    assert not server.server.adapters
    clock_records(server, tmp_path)


def test_hd09_header_handling_consumes_capacity_before_body_or_adapter(tmp_path):
    counter = HttpCounter()
    server = BoundedReceiver(
        counter, ledger(tmp_path), capacity=2, response_budget=1, output_reserve=0.2
    )
    clients = []
    try:
        server.start()
        for _ in range(2):
            raw = owned_socket(server)
            clients.append(raw)
            raw.sendall(b"POST /partial")
        wait_for(lambda: server.active == 2)
        assert send(server, body=b"{}")[0] == 503
        assert len(server.server.leases) == 2 and not server.server.adapters and counter.calls == 0
    finally:
        for raw in clients:
            raw.close()
        assert server.stop(3)["stopped"]
    assert all(not row["workers_alive"] for row in clock_records(server, tmp_path))


def test_hd10_actual_output_backpressure_is_not_a_completed_response(tmp_path, monkeypatch):
    class LargeResponse(receiver_module.Handler):
        def do_GET(self):
            self.respond(200, b"x" * (4 * 1024 * 1024))

    original = receiver_module.Server.get_request

    def small_buffer(server):
        connection, address = original(server)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        return connection, address

    monkeypatch.setattr(receiver_module, "Handler", LargeResponse)
    monkeypatch.setattr(receiver_module.Server, "get_request", small_buffer)
    server = BoundedReceiver(None, ledger(tmp_path), response_budget=0.8, output_reserve=0.2)
    raw = None
    try:
        server.start()
        raw = socket.socket()
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        raw.settimeout(3)
        raw.connect(server.address)
        raw.sendall(b"GET /authored-output HTTP/1.1\r\nHost: fixture\r\n\r\n")
        wait_for(
            lambda: server.server.leases and server.server.leases[0].response_started_at is not None
        )
        assert server.stop(3)["stopped"]
        wire = wire_until_closed(raw)
        lease = server.server.leases[0]
        assert not lease.response_completed and lease.response_completed_at is None
        assert wire.count(b"HTTP/1.0 ") == 1 and len(wire) < 4 * 1024 * 1024
        # This bounds failed-output cleanup, not a successful late response.
        assert lease.response_finished_at - lease.connection.accepted_at <= 1.05
    finally:
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    clock_records(server, tmp_path)


@pytest.mark.parametrize("delay,expected", [(0.01, 200), (0.7, 503)])
def test_hd11_watch_and_adapter_send_only_one_response(tmp_path, delay, expected):
    counter = HttpCounter(delay)
    server = BoundedReceiver(counter, ledger(tmp_path), response_budget=0.8, output_reserve=0.2)
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        raw.sendall(request_bytes(server))
        wire = wire_until_closed(raw)
        assert wire.startswith(f"HTTP/1.0 {expected} ".encode()) and wire.count(b"HTTP/1.0 ") == 1
    finally:
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert counter.calls == 1
    clock_records(server, tmp_path)


@pytest.mark.parametrize("role", ["watcher", "handler"])
def test_hd12_owned_start_failure_does_not_leak_worker_or_exception(
    tmp_path, monkeypatch, capsys, role
):
    record = ledger(tmp_path)
    counter = HttpCounter()
    server = BoundedReceiver(counter, record, response_budget=0.8, output_reserve=0.2)
    raw = None
    original = threading.Thread.start
    prefix = "owned-deadline-" if role == "watcher" else "owned-http-"

    def failed(thread):
        if thread.name.startswith(prefix) and thread.name != "owned-http-accept":
            raise RuntimeError("PRIVATE_START_EXCEPTION")
        return original(thread)

    try:
        server.start()
        monkeypatch.setattr(threading.Thread, "start", failed)
        raw = owned_socket(server)
        raw.sendall(request_bytes(server))
        wire = wire_until_closed(raw)
        wait_for(lambda: bool(server.server.leases) and server.active == 0)
        assert b"PRIVATE_START_EXCEPTION" not in wire and counter.calls == 0
    finally:
        monkeypatch.setattr(threading.Thread, "start", original)
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    output = capsys.readouterr()
    assert (
        "PRIVATE_START_EXCEPTION" not in output.out + output.err and "Traceback" not in output.err
    )
    assert b"PRIVATE_START_EXCEPTION" not in record.path.read_bytes()
    clock_records(server, tmp_path)


@pytest.mark.parametrize(
    "options",
    [
        {"response_budget": None},
        {"response_budget": 0},
        {"response_budget": 4.6},
        {"response_budget": math.nan},
        {"response_budget": True},
        {"output_reserve": None},
        {"output_reserve": 0},
        {"output_reserve": -1},
        {"response_budget": 0.8, "output_reserve": 0.8},
        {"output_reserve": math.inf},
    ],
)
def test_hd13_invalid_absolute_budget_refuses_before_bind(tmp_path, options):
    before = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(DriverRefused):
        BoundedReceiver(None, ledger(tmp_path), **options)
    assert {thread.ident for thread in threading.enumerate()} == before


def test_hd14_blocked_diagnostics_cannot_delay_response_or_launch_adapter(tmp_path):
    record = ledger(tmp_path)
    entered, release = threading.Event(), threading.Event()

    class SlowDiagnostics:
        run_id = record.run_id

        def request(self, *args):
            return record.request(*args)

        def observe(self, kind, **fields):
            if kind == "request_received":
                entered.set()
                release.wait(2)
            return record.observe(kind, **fields)

    counter = HttpCounter()
    server = BoundedReceiver(counter, SlowDiagnostics(), response_budget=0.6, output_reserve=0.15)
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        raw.sendall(request_bytes(server))
        assert entered.wait(1)
        assert wire_until_closed(raw).startswith(b"HTTP/1.0 503 ")
        assert time.monotonic() - server.server.leases[0].connection.accepted_at <= 0.6
        assert not server.stop(0.01)["stopped"]
    finally:
        release.set()
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert counter.calls == 0 and not server.server.adapters
    clock_records(server, tmp_path)


def test_hd14b_actual_ingest_result_precedes_blocked_finished_diagnostics(rig, tmp_path):
    task = rig.task()
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    record = ledger(tmp_path)
    entered, release = threading.Event(), threading.Event()

    class SlowFinished:
        run_id = record.run_id

        def request(self, *args):
            return record.request(*args)

        def observe(self, kind, **fields):
            if kind == "adapter_finished":
                entered.set()
                release.wait(2)
            return record.observe(kind, **fields)

    server = BoundedReceiver(rig.receiver, SlowFinished(), response_budget=0.8, output_reserve=0.2)
    try:
        server.start()
        assert send(server, headers, body)[0] == 200
        assert entered.wait(1) and len(facts(rig)["inbox"]) == 1
        assert server.active == 1 and not server.stop(0.01)["stopped"]
    finally:
        release.set()
        assert server.stop(3)["stopped"]
    assert clock_records(server, tmp_path)[0]["response_completed"]


@pytest.mark.parametrize("shape", ["single_line", "cumulative"])
def test_hd16_bounded_header_prefix_refuses_over_limit_input(tmp_path, shape):
    counter, record = HttpCounter(), ledger(tmp_path)
    server = BoundedReceiver(counter, record, response_budget=0.8, output_reserve=0.2)
    raw = None
    try:
        server.start()
        raw = owned_socket(server)
        data = f"POST {server.route} HTTP/1.1\r\nX-Private: ".encode() + b"z" * 66000
        if shape == "cumulative":
            data = (
                f"POST {server.route} HTTP/1.1\r\nContent-Length: 0\r\n".encode()
                + b"".join(f"X-{number}: ".encode() + b"z" * 850 + b"\r\n" for number in range(80))
                + b"\r\n"
            )
        try:
            raw.sendall(data)
        except OSError:
            pass
        assert wire_until_closed(raw).startswith(b"HTTP/1.0 431 ")
        assert counter.calls == 0
    finally:
        if raw is not None:
            raw.close()
        assert server.stop(3)["stopped"]
    assert b"z" * 1000 not in record.path.read_bytes()
    clock_records(server, tmp_path)


def startup_case_worker(folder, scenario, queue):
    """All dangerous lifecycle calls stay in this owned child, never pytest main."""
    folder = Path(folder)
    original_stderr = sys.stderr
    captured_stderr = (folder / "startup-stderr.log").open("w")
    sys.stderr = captured_stderr
    server = BoundedReceiver(HttpCounter(), ledger(folder))
    original_start = threading.Thread.start
    original_bind = server.server.server_bind
    original_activate = server.server.server_activate
    original_serve = server.server.serve_forever
    entered, release = threading.Event(), threading.Event()
    facts_out, caller = {}, None

    def fail():
        raise RuntimeError("PRIVATE_AUTHORED_START_FAILURE")

    def attempt_start():
        try:
            server.start()
        except BaseException as error:
            facts_out["start_error"] = type(error).__name__

    def snapshot():
        return {
            "socket_closed": server.server.socket.fileno() == -1,
            "accept_alive": server.thread is not None and server.thread.is_alive(),
            "accept_ident": server.thread.ident if server.thread else None,
            "shutdown_alive": server.shutdown_thread is not None
            and server.shutdown_thread.is_alive(),
            "shutdown_created": server.shutdown_thread is not None,
            "loop_witness": server.loop_entered.is_set(),
            "startup_rollback": server.startup_rollback,
        }

    try:
        if scenario in {"SS02", "SS05"}:

            def failed_start(thread):
                if thread.name == "owned-http-accept":
                    if scenario == "SS05":
                        original_start(thread)
                    fail()
                return original_start(thread)

            threading.Thread.start = failed_start
            attempt_start()
            facts_out["after_error"] = snapshot()
            facts_out["stop"] = server.stop(0.05)
        elif scenario in {"SS03", "SS04"}:
            if scenario == "SS03":
                server.server.server_bind = fail
            else:
                server.server.server_activate = fail
            attempt_start()
            facts_out["after_error"] = snapshot()
            facts_out["stop"] = server.stop(0.05)
            attempt_start()
            facts_out["retry_error"] = facts_out.get("start_error")
        elif scenario == "SS06":
            facts_out["stop"] = server.stop(0.05)
            attempt_start()
        elif scenario == "SS07":
            server.start()
            first_ident = server.thread.ident
            attempt_start()
            facts_out["duplicate_error"] = facts_out.get("start_error")
            facts_out["same_thread"] = server.thread.ident == first_ident
            facts_out["stop"] = server.stop(1)
            attempt_start()
        elif scenario == "SS08":

            def delayed_activate():
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("authored_gate_timeout")
                original_activate()

            server.server.server_activate = delayed_activate
            caller = threading.Thread(target=attempt_start, name="owned-startup-caller")
            caller.start()
            assert entered.wait(1)
            before = time.monotonic()
            facts_out["first_stop"] = server.stop(0.02)
            facts_out["first_stop_seconds"] = time.monotonic() - before
            release.set()
            caller.join(1)
            facts_out["caller_alive"] = caller.is_alive()
            facts_out["stop"] = server.stop(1)
        elif scenario == "SS09":
            server.start()
            assert server.loop_entered.wait(1)

            def failed_shutdown(thread):
                if thread.name == "owned-http-shutdown":
                    fail()
                return original_start(thread)

            threading.Thread.start = failed_shutdown
            facts_out["first_stop"] = server.stop(0.05)
            facts_out["retained"] = snapshot()
            threading.Thread.start = original_start
            facts_out["stop"] = server.stop(1)
        elif scenario == "SS10":

            def failed_loop(*args, **kwargs):
                fail()

            server.server.serve_forever = failed_loop
            attempt_start()
            if server.thread is not None:
                server.thread.join(1)
            facts_out["before_stop"] = snapshot()
            facts_out["stop"] = server.stop(0.05)
        elif scenario == "SS11":

            def delayed_loop(*args, **kwargs):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("authored_gate_timeout")
                return original_serve(*args, **kwargs)

            server.server.serve_forever = delayed_loop
            server.start()
            assert entered.wait(1)
            before = time.monotonic()
            facts_out["first_stop"] = server.stop(0.02)
            facts_out["first_stop_seconds"] = time.monotonic() - before
            facts_out["before_entry"] = snapshot()
            release.set()
            assert server.loop_entered.wait(1)
            facts_out["stop"] = server.stop(1)
        else:
            assert scenario == "SS01"
            server.start()
            assert send(server, method="GET", route=server.health_route)[0] == 200
            facts_out["stop"] = server.stop(1)
            facts_out["retry_stop"] = server.stop(0.05)
        facts_out["final_before_helper_cleanup"] = snapshot()
    except BaseException as error:
        facts_out["worker_error"] = type(error).__name__
    finally:
        threading.Thread.start = original_start
        server.server.server_bind = original_bind
        server.server.server_activate = original_activate
        server.server.serve_forever = original_serve
        release.set()
        if caller is not None:
            caller.join(1)
        # Preserve desired facts before independent helper cleanup.
        (folder / "startup-observation.json").write_text(
            json.dumps(facts_out, indent=2, sort_keys=True) + "\n"
        )
        if (
            server.shutdown_thread is not None
            and server.shutdown_thread.is_alive()
            and (server.thread is None or server.thread.ident is None)
        ):
            cleanup_loop = threading.Thread(
                target=lambda: original_serve(poll_interval=0.01),
                name="owned-startup-helper-cleanup",
            )
            cleanup_loop.start()
            cleanup_loop.join(1)
        facts_out["helper_cleanup"] = server.stop(1)
        facts_out["helper_final"] = snapshot()
        captured_stderr.flush()
        facts_out["private_exception_in_stderr"] = (
            b"PRIVATE_AUTHORED_START_FAILURE" in (folder / "startup-stderr.log").read_bytes()
        )
        facts_out["diagnostics"] = server.ledger.summary()["observations"]
        sys.stderr = original_stderr
        captured_stderr.close()
        (folder / "startup-result.json").write_text(
            json.dumps(facts_out, indent=2, sort_keys=True) + "\n"
        )
        queue.put(facts_out)


@pytest.mark.parametrize("scenario", [f"SS{number:02}" for number in range(1, 12)])
def test_ss01_to_ss11_startup_lifecycle_in_owned_child(tmp_path, scenario):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=startup_case_worker, args=(str(tmp_path), scenario, queue))
    try:
        child.start()
        facts_out = queue.get(timeout=5)
        child.join(timeout=1)
        assert child.exitcode == 0 and not child.is_alive()
        assert "worker_error" not in facts_out
        assert not facts_out["private_exception_in_stderr"]
        assert facts_out["stop"]["stopped"]
        assert facts_out["helper_cleanup"]["stopped"]
        assert facts_out["helper_final"]["socket_closed"]
        assert not facts_out["helper_final"]["accept_alive"]
        assert not facts_out["helper_final"]["shutdown_alive"]
        if scenario in {"SS02", "SS05"}:
            assert facts_out["start_error"] == "RuntimeError"
            assert facts_out["after_error"]["startup_rollback"]["stopped"]
            assert facts_out["after_error"]["socket_closed"]
        if scenario == "SS02":
            assert not facts_out["after_error"]["shutdown_created"]
        if scenario in {"SS03", "SS04", "SS06", "SS07", "SS08"}:
            assert facts_out["start_error"] == "DriverRefused"
        if scenario in {"SS03", "SS04"}:
            assert facts_out["after_error"]["socket_closed"]
            assert not facts_out["after_error"]["shutdown_created"]
        if scenario == "SS07":
            assert facts_out["same_thread"] and facts_out["duplicate_error"] == "DriverRefused"
        if scenario == "SS08":
            assert (
                not facts_out["first_stop"]["stopped"]
                and facts_out["first_stop"]["lifecycle_pending"]
            )
            assert facts_out["first_stop_seconds"] <= 0.2 and not facts_out["caller_alive"]
        if scenario == "SS09":
            assert (
                not facts_out["first_stop"]["stopped"]
                and facts_out["first_stop"]["shutdown_start_failed"]
            )
            assert (
                facts_out["retained"]["accept_alive"] and not facts_out["retained"]["socket_closed"]
            )
        if scenario == "SS10":
            assert (
                not facts_out["before_stop"]["accept_alive"]
                and not facts_out["before_stop"]["loop_witness"]
            )
            assert not facts_out["final_before_helper_cleanup"]["shutdown_created"]
            assert any(
                row["kind"] == "server_failed"
                and json.loads(row["payload"])["code"] == "server_exception"
                for row in facts_out["diagnostics"]
            )
        if scenario == "SS11":
            assert not facts_out["first_stop"]["stopped"] and facts_out["first_stop_seconds"] <= 0.2
            assert (
                facts_out["before_entry"]["accept_alive"]
                and not facts_out["before_entry"]["loop_witness"]
            )
            assert not facts_out["before_entry"]["shutdown_created"]
            assert facts_out["final_before_helper_cleanup"]["loop_witness"]
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.terminate()
            child.join(timeout=1)
            if child.is_alive():
                child.kill()
                child.join(timeout=1)
        assert not child.is_alive()
        queue.close()
        queue.join_thread()
