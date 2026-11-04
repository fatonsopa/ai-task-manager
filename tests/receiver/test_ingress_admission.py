"""Deterministic authored ingress races through real receiver and drain threads."""

import json
import sqlite3
import threading
import time

import pytest

from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from tests.linear.helpers import STATES, uid
from tests.receiver.helpers import ROUTE, Fixture, request, wait_for


@pytest.fixture
def fixture(tmp_path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.close()
        assert not value.provider.thread.is_alive()


class AdmissionRace:
    """Hold the actual producer after journal acceptance and before Core admission."""

    def __init__(self, fixture, monkeypatch):
        self.fixture = fixture
        self.service = fixture.load()
        self.receiver = InstalledReceiver(self.service, fixture.config)
        self.snapshot_taken = threading.Event()
        self.release_snapshot = threading.Event()
        self.producer_waiting = threading.Event()
        self.release_producer = threading.Event()
        self.consumer_finished = threading.Event()
        self.trace = []
        self.clients = []
        self.responses = []
        self.errors = []
        self.finish = None
        self.before_release = None
        self.after_release = None
        self.snapshot_blocked = False
        journal = self.service.inbound.journal
        original_rows = journal.deliveries
        original_ingest = self.service.core.sync.ingest
        original_finish = journal.finish_delivery

        def rows(scope):
            result = original_rows(scope)
            if threading.current_thread() is self.receiver.worker and not self.snapshot_blocked:
                self.snapshot_blocked = True
                self.note("startup_repair_snapshot", receipt_count=len(result))
                self.snapshot_taken.set()
                assert self.release_snapshot.wait(8), "Finite startup snapshot gate"
            return result

        def ingest(*args, **kwargs):
            producer = threading.current_thread() is not self.receiver.worker
            if producer:
                self.note("producer_before_core_ingest")
                self.producer_waiting.set()
                assert self.release_producer.wait(8), "Finite producer admission gate"
            self.note("producer_core_ingest" if producer else "consumer_core_ingest")
            result = original_ingest(*args, **kwargs)
            self.note("core_ingest_returned", inserted=result, producer=producer)
            return result

        def finish(receipt_id, owner, state, now, error=None):
            result = original_finish(receipt_id, owner, state, now, error)
            self.finish = {"state": state, "error": error}
            self.note("consumer_finished", **self.finish)
            self.consumer_finished.set()
            return result

        monkeypatch.setattr(journal, "deliveries", rows)
        monkeypatch.setattr(self.service.core.sync, "ingest", ingest)
        monkeypatch.setattr(journal, "finish_delivery", finish)

    def note(self, stage, **values):
        self.trace.append(
            {"stage": stage, "thread": threading.get_ident(), "at": time.monotonic(), **values}
        )

    def start(self):
        self.receiver.start()
        assert self.snapshot_taken.wait(3)
        assert self.trace[0]["receipt_count"] == 0

    def send(self, *, headers=None, body=None):
        if headers is None:
            payload = self.fixture.provider.event(
                self.fixture.task.issue_id, timestamp=int(time.time() * 1000)
            )
            headers, body = self.fixture.rig.signed(payload, uid(182))

        def client():
            started = time.monotonic()
            try:
                response = request(self.receiver.http.address, ROUTE, headers, body)[0]
                self.responses.append({"status": response, "seconds": time.monotonic() - started})
            except BaseException as error:
                self.errors.append(type(error).__name__)

        thread = threading.Thread(target=client, name="authored-ingress-client")
        self.clients.append(thread)
        thread.start()
        assert self.producer_waiting.wait(3)
        return headers, body

    def facts(self):
        rows = self.service.inbound.journal.deliveries(self.service.scope)
        return {
            "receipts": rows,
            "inbox": self.service.core.sync.inbound(self.service.handle),
            "proof": self.service.inbound.journal.verify_evidence(rows[0]["ingress_ref"]).payload,
            "events": [
                e.kind for e in self.service.core.notifications.list_events(self.service.handle)
            ],
        }

    def close(self):
        self.release_snapshot.set()
        self.release_producer.set()
        for thread in self.clients:
            thread.join(timeout=6)
            assert not thread.is_alive()
        cleanup = self.receiver.stop()
        assert cleanup["stopped"] and cleanup["drain_stopped"] and cleanup["ownership_released"]
        assert not self.receiver.worker.is_alive()
        evidence = {
            "trace": self.trace,
            "responses": self.responses,
            "errors": self.errors,
            "before_producer_release": self.before_release,
            "after_producer_release": self.after_release,
            "cleanup": cleanup,
        }
        (self.fixture.root / "admission-race.json").write_text(json.dumps(evidence, indent=2))
        return cleanup


@pytest.mark.parametrize("kind", ["echo", "human", "duplicate"])
def test_ia01_arrival_after_startup_snapshot_precedes_producer_core_commit(
    fixture, monkeypatch, kind
):
    if kind == "echo":
        fixture.task = fixture.rig.transition(fixture.task, "Todo", "enqueue")
    trial = AdmissionRace(fixture, monkeypatch)
    try:
        trial.start()
        if kind != "echo":
            fixture.provider.edit(
                fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"}
            )
        headers, body = trial.send()
        if kind == "duplicate":
            trial.send(headers=headers, body=body)
            wait_for(
                lambda: sum(t["stage"] == "producer_before_core_ingest" for t in trial.trace) == 2
            )
        assert len(trial.service.inbound.journal.deliveries(trial.service.scope)) == 1
        assert trial.service.core.sync.inbound(trial.service.handle) == []
        trial.release_snapshot.set()
        assert trial.consumer_finished.wait(3), trial.trace
        trial.before_release = trial.facts()
        trial.release_producer.set()
        for client in trial.clients:
            client.join(timeout=3)
            assert not client.is_alive()
        trial.after_release = trial.facts()
        assert not trial.errors and all(r["status"] == 200 for r in trial.responses)
        assert all(r["seconds"] < 5 for r in trial.responses)
        assert trial.finish == {"state": "applied", "error": None}
        assert trial.before_release["inbox"][0]["state"] == "applied"
        assert trial.after_release == trial.before_release
        assert len(trial.after_release["receipts"]) == 1
        assert trial.after_release["events"].count("external_change_applied") == (
            0 if kind == "echo" else 1
        )
        assert trial.service.core.get_task(
            trial.service.handle, fixture.task.id
        ).decision_revision == (0 if kind == "echo" else 1)
    finally:
        assert trial.close()["failure"] is None


def accept_without_core(fixture, service, receiver, monkeypatch):
    """Authenticate normally; model a producer interruption after journal commit."""
    fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    headers, body = fixture.event()

    def interrupted(*args, **kwargs):
        raise sqlite3.OperationalError("Authored interruption before Core admission")

    with monkeypatch.context() as patch:
        patch.setattr(service.core.sync, "ingest", interrupted)
        assert receiver.receive("POST", headers, body).status == 503
    rows = service.inbound.journal.deliveries(service.scope)
    assert len(rows) == 1 and rows[0]["state"] == "received"
    assert not service.core.sync.inbound(service.handle)
    return rows[0]


@pytest.mark.parametrize("fault", ["revoked", "expired"])
def test_ia02_authority_lost_after_claim_prevents_consumer_admission(fixture, monkeypatch, fault):
    from ai_task_manager.domains.work.errors import AccessDenied
    from tests.receiver.test_authority_boundary import revoke

    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    original = accept_without_core(fixture, service, receiver, monkeypatch)
    original_claim = service.inbound.journal.claim_delivery

    def claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        if fault == "revoked":
            revoke(receiver)
        else:
            receiver.observations.journal.clock = lambda: time.time() + 3600
        return result

    monkeypatch.setattr(service.inbound.journal, "claim_delivery", claim)
    requests = len(fixture.provider.requests)
    before = service.core.get_task(service.handle, fixture.task.id)
    try:
        with pytest.raises(AccessDenied):
            service.inbound.run_once(service.handle, check_authority=receiver.check)
        assert service.core.sync.inbound(service.handle) == []
        assert service.core.get_task(service.handle, fixture.task.id) == before
        assert len(fixture.provider.requests) == requests
        row = service.inbound.journal.deliveries(service.scope)[0]
        assert row["state"] == "received" and row["owner"] is None and row["lease_until"] is None
        assert (
            row["payload"] == original["payload"] and row["ingress_ref"] == original["ingress_ref"]
        )
    finally:
        assert receiver.stop()["stopped"]


@pytest.mark.parametrize("fault", ["missing_proof", "payload_mismatch", "foreign_proof"])
def test_ia03_consumer_requires_original_scoped_ingress_proof(fixture, monkeypatch, fault):
    from dataclasses import replace

    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    original = accept_without_core(fixture, service, receiver, monkeypatch)
    original_claim = service.inbound.journal.claim_delivery
    foreign_ref = None
    if fault == "foreign_proof":
        # A genuine journal-issued proof for a different scope cannot admit this receipt.
        scope = replace(service.scope, logical_session="foreign-authored-session")
        payload = json.loads(original["payload"])
        headers, body = fixture.rig.signed(payload, original["delivery_id"])
        foreign, _ = service.inbound.journal.accept_delivery(
            scope, original["delivery_id"], body, payload
        )
        foreign_ref = foreign["ingress_ref"]

    def claim(*args, **kwargs):
        result = dict(original_claim(*args, **kwargs))
        if fault == "missing_proof":
            result["ingress_ref"] = "linear-proof:unissued"
        elif fault == "foreign_proof":
            result["ingress_ref"] = foreign_ref
        else:
            payload = json.loads(result["payload"])
            payload["actor"]["id"] = uid(999)
            result["payload"] = json.dumps(payload)
        return result

    monkeypatch.setattr(service.inbound.journal, "claim_delivery", claim)
    before = service.core.get_task(service.handle, fixture.task.id)
    requests = len(fixture.provider.requests)
    try:
        result = service.inbound.run_once(service.handle, check_authority=receiver.check)
        assert result["state"] == "conflict"
        assert result["error"] in {"EvidenceRequired", "ScopeMismatch"}
        assert not service.core.sync.inbound(service.handle)
        assert service.core.get_task(service.handle, fixture.task.id) == before
        assert len(fixture.provider.requests) == requests
        row = service.inbound.journal.deliveries(service.scope)[0]
        assert row["state"] == "conflict" and row["payload"] == original["payload"]
        assert row["ingress_ref"] == original["ingress_ref"]
    finally:
        assert receiver.stop()["stopped"]


def test_ia04_actual_core_lock_delays_admission_without_creating_conflict(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    original = accept_without_core(fixture, service, receiver, monkeypatch)
    entered, proceed, begin_waiting, done = [threading.Event() for _ in range(4)]
    original_ingest = service.core.sync.ingest
    original_connect = service.core.context.db.connect
    result, errors = [], []

    def ingest(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return original_ingest(*args, **kwargs)

    def connect():
        conn = original_connect()
        if threading.current_thread() is consumer and entered.is_set():
            conn.set_trace_callback(
                lambda sql: begin_waiting.set() if sql == "BEGIN IMMEDIATE" else None
            )
        return conn

    def drain():
        try:
            result.append(service.inbound.run_once(service.handle, check_authority=receiver.check))
        except BaseException as error:
            errors.append(type(error).__name__)
        finally:
            done.set()

    monkeypatch.setattr(service.core.sync, "ingest", ingest)
    monkeypatch.setattr(service.core.context.db, "connect", connect)
    consumer = threading.Thread(target=drain, name="authored-admission-contention")
    consumer.start()
    lock = None
    started = time.monotonic()
    requests = len(fixture.provider.requests)
    try:
        assert entered.wait(3)
        lock = original_connect()
        lock.execute("BEGIN IMMEDIATE")  # Hold ownership only; no manual Core mutation.
        proceed.set()
        assert begin_waiting.wait(3)
        assert not done.is_set() and len(fixture.provider.requests) == requests
        lock.rollback()
        lock.close()
        lock = None
        assert done.wait(3), errors
        consumer.join(timeout=1)
        assert not consumer.is_alive() and not errors
        assert result[0]["state"] == "applied"
        assert time.monotonic() - started < 5
        assert service.core.sync.inbound(service.handle)[0]["state"] == "applied"
        row = service.inbound.journal.deliveries(service.scope)[0]
        assert row["id"] == original["id"] and row["state"] == "applied"
        assert (
            sum(
                e.kind == "external_change_applied"
                for e in service.core.notifications.list_events(service.handle)
            )
            == 1
        )
    finally:
        proceed.set()
        if lock:
            lock.rollback()
            lock.close()
        consumer.join(timeout=12)
        assert not consumer.is_alive()
        assert receiver.stop()["stopped"]


def test_ia05_restart_after_admission_io_failure_reuses_receipt(fixture, monkeypatch):
    service = fixture.load()
    first = InstalledReceiver(service, fixture.config)
    original = accept_without_core(fixture, service, first, monkeypatch)
    journal = service.inbound.journal
    claim = journal.claim_delivery

    def short_claim(*args, **kwargs):
        return claim(*args, **kwargs, lease_seconds=0.1)

    def interrupted(*args, **kwargs):
        raise sqlite3.OperationalError("Authored failed consumer admission transaction")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(journal, "claim_delivery", short_claim)
            patch.setattr(service.core.sync, "ingest", interrupted)
            with pytest.raises(sqlite3.OperationalError):
                service.inbound.run_once(service.handle, check_authority=first.check)
        row = journal.deliveries(service.scope)[0]
        assert row["state"] == "leased" and row["error"] is None
        assert not service.core.sync.inbound(service.handle)
    finally:
        assert first.stop()["stopped"]
    second = InstalledReceiver(fixture.load(), fixture.config)
    try:
        second.start()
        wait_for(lambda: journal.deliveries(service.scope)[0]["state"] == "applied")
        row = journal.deliveries(service.scope)[0]
        assert row["id"] == original["id"] and row["payload"] == original["payload"]
        assert row["raw_digest"] == original["raw_digest"]
        assert len(second.service.core.sync.inbound(second.service.handle)) == 1
        assert (
            sum(
                e.kind == "external_change_applied"
                for e in second.service.core.notifications.list_events(second.service.handle)
            )
            == 1
        )
    finally:
        cleanup = second.stop()
        (fixture.root / "restart-cleanup.json").write_text(json.dumps(cleanup))
        assert cleanup["stopped"] and cleanup["ownership_released"] and cleanup["failure"] is None
        assert not second.worker.is_alive()
