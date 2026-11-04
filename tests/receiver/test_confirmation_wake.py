"""Scoped local recovery with separate outbound processes and an authored provider."""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.config.service import load_service
from tests.linear.helpers import STATES, uid

from .helpers import ROUTE, Fixture, request, wait_for


@pytest.fixture
def fixture(tmp_path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.close()
        assert not value.provider.thread.is_alive()


class Trial:
    def __init__(self, fixture, monkeypatch, *, mode="normal"):
        self.fixture = fixture
        self.entered, self.release = threading.Event(), threading.Event()
        self.service = fixture.load()
        self.receiver = InstalledReceiver(self.service, fixture.config)
        self.receiver.start()
        self.calls = 0
        original = getattr(self.service.inbound, "next_receipt", None)

        def counted(*args):
            self.calls += 1
            return original(*args)

        if original is not None:
            monkeypatch.setattr(self.service.inbound, "next_receipt", counted)
        self.task = fixture.rig.transition(fixture.task, "Todo", "enqueue", confirm=False)

        def hold(request, _):
            if "mutation UpdateIssue" in request["query"]:
                self.entered.set()
                assert self.release.wait(12), "Finite authored response gate"

        fixture.provider.hook = hold
        self.child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                __name__,
                str(fixture.config),
                fixture.provider.endpoint,
                self.task.id,
                mode,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(Path.cwd()),
                        os.environ.get("AITM_WAKE_TEST_SOURCE", str(Path.cwd() / "src")),
                        os.environ.get("PYTHONPATH", ""),
                    ]
                ),
            },
            start_new_session=True,
        )
        assert self.entered.wait(8)

    def accept(self):
        self.headers, self.body = self.fixture.event()
        assert request(self.receiver.http.address, ROUTE, self.headers, self.body)[0] == 200
        self.receipt = wait_for(
            lambda: next(
                (
                    r
                    for r in self.rows()
                    if r["state"] == "received" and json.loads(r["checkpoint"]).get("snapshot")
                ),
                None,
            )
        )
        return self.receipt

    def rows(self):
        return self.service.inbound.journal.deliveries(self.service.scope)

    def confirm(self, expected="confirmed"):
        self.release.set()
        output, error = self.child.communicate(timeout=10)
        assert self.child.returncode == 0 and not error, (output, error)
        result = json.loads(output)
        assert result["state"] == expected, result
        return result

    def close(self):
        self.release.set()
        if self.child.poll() is None:
            self.child.communicate(timeout=10)
        cleanup = self.receiver.stop()
        assert cleanup["stopped"] and cleanup["drain_stopped"] and cleanup["ownership_released"]
        assert not self.receiver.worker.is_alive()
        with pytest.raises(ProcessLookupError):
            os.kill(self.child.pid, 0)
        (self.fixture.root / "wake-cleanup.json").write_text(
            json.dumps(
                {
                    "receiver": cleanup,
                    "child_pid": self.child.pid,
                    "returncode": self.child.returncode,
                    "reaped": True,
                }
            )
        )


@pytest.mark.parametrize("mode", ["running", "lost_notification", "restart", "before_intake"])
def test_cw01_separate_process_confirmation_resumes_original_receipt(fixture, monkeypatch, mode):
    trial = Trial(fixture, monkeypatch, mode="lost" if mode == "lost_notification" else "normal")
    try:
        if mode != "before_intake":
            original = trial.accept()
            assert len(trial.rows()) == 1
        if mode == "restart":
            assert trial.receiver.stop()["stopped"]
        begin = time.monotonic()
        confirmed = trial.confirm()
        if mode == "before_intake":
            headers, body = fixture.event()
            assert request(trial.receiver.http.address, ROUTE, headers, body)[0] == 200
            original = trial.rows()[0]
        if mode == "restart":
            trial.service = fixture.load()
            trial.receiver = InstalledReceiver(trial.service, fixture.config)
            trial.receiver.start()
        final = wait_for(
            lambda: next((r for r in trial.rows() if r["state"] == "applied"), None), 3
        )
        elapsed = time.monotonic() - begin
        assert final["id"] == original["id"]
        assert final["payload"] == original["payload"]
        assert final["raw_digest"] == original["raw_digest"]
        assert len(trial.rows()) == 1
        assert trial.receiver._recovery_delay() is None
        if mode == "lost_notification":
            with trial.service.inbound.journal.transaction() as db:
                assert (
                    db.execute("SELECT COUNT(*) FROM history WHERE stage='wake_failed'").fetchone()[
                        0
                    ]
                    == 1
                )
        task = trial.service.core.get_task(trial.service.handle, trial.task.id)
        assert task.observed_status == "Todo" and task.decision_revision == 0
        assert not trial.service.core.external.observations(trial.service.handle, task.id)
        assert len(trial.service.inbound.journal.sends(trial.service.scope)) == 2
        assert sum("mutation UpdateIssue" in q["query"] for q, _ in fixture.provider.requests) == 1
        events = trial.service.core.notifications.list_events(trial.service.handle)
        assert not any(e.kind == "external_change_applied" for e in events)
        # Replayed authentic bytes remain the same applied receipt and decision.
        if mode != "before_intake":
            assert request(trial.receiver.http.address, ROUTE, trial.headers, trial.body)[0] == 200
            assert trial.service.core.get_task(trial.service.handle, task.id) == task
        (fixture.root / "wake-timing.json").write_text(
            json.dumps(
                {
                    "mode": mode,
                    "release_to_observation_seconds": elapsed,
                    "confirmation_to_observation_seconds": time.monotonic()
                    - confirmed["completed_at"],
                    "receipt_id": final["id"],
                    "outbound_pid": trial.child.pid,
                    "receiver_pid": os.getpid(),
                    "second_event_before_application": False,
                }
            )
        )
    finally:
        trial.close()


def test_cw02_pending_receipt_wait_is_bounded_without_provider_polling(fixture, monkeypatch):
    trial = Trial(fixture, monkeypatch)
    try:
        trial.accept()
        baseline = len(fixture.provider.requests)
        calls = trial.calls
        time.sleep(0.85)
        assert len(fixture.provider.requests) == baseline
        assert 2 <= trial.calls - calls <= 8
        assert trial.rows()[0]["state"] == "received"
        trial.confirm()
        wait_for(lambda: trial.rows()[0]["state"] == "applied")
    finally:
        trial.close()


def test_cw03_newer_human_event_is_not_starved_by_parked_echo(fixture, monkeypatch):
    trial = Trial(fixture, monkeypatch)
    try:
        original = trial.accept()
        fixture.provider.edit(
            trial.task.issue_id, state={"id": STATES["Backlog"], "name": "Backlog"}
        )
        headers, body = fixture.event()
        assert request(trial.receiver.http.address, ROUTE, headers, body)[0] == 200
        current = wait_for(
            lambda: (
                t
                if (
                    t := trial.service.core.get_task(trial.service.handle, trial.task.id)
                ).desired_status
                == "Backlog"
                else None
            )
        )
        assert current.observed_status == "Backlog" and current.decision_revision == 1
        operation = trial.service.core.sync.list_operations(trial.service.handle)[-1]
        assert operation.fenced_by
        trial.confirm("uncertain")
        assert (
            trial.service.core.get_task(trial.service.handle, current.id).desired_status
            == "Backlog"
        )
        assert next(r for r in trial.rows() if r["id"] == original["id"])["state"] == "conflict"
        assert len([r for r in trial.rows() if r["state"] == "applied"]) == 1
        assert sum("mutation UpdateIssue" in q["query"] for q, _ in fixture.provider.requests) == 1
    finally:
        trial.close()


def test_cw04_other_issue_progresses_while_first_echo_waits(fixture, monkeypatch):
    other = fixture.rig.task()
    trial = Trial(fixture, monkeypatch)
    try:
        original = trial.accept()
        fixture.provider.edit(other.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        headers, body = fixture.rig.signed(
            fixture.provider.event(other.issue_id, timestamp=int(time.time() * 1000))
        )
        assert request(trial.receiver.http.address, ROUTE, headers, body)[0] == 200
        wait_for(
            lambda: (
                trial.service.core.get_task(trial.service.handle, other.id).observed_status
                == "Todo"
            )
        )
        assert next(r for r in trial.rows() if r["id"] == original["id"])["state"] == "received"
        trial.confirm()
        wait_for(lambda: all(r["state"] == "applied" for r in trial.rows()))
    finally:
        trial.close()


def test_cw05_revocation_before_recovery_prevents_application(fixture, monkeypatch):
    trial = Trial(fixture, monkeypatch)
    try:
        trial.accept()
        bind_record(
            trial.receiver.observations.journal,
            "session-revoked:" + digest(trial.service.grant_ref),
            {"scope": {}, "reason": "authored wake-boundary revocation"},
        )
        wait_for(lambda: trial.receiver.failed)
        trial.confirm()
        assert trial.rows()[0]["state"] == "received"
        assert not trial.service.core.external.observations(trial.service.handle, trial.task.id)
        assert trial.receiver.failed == "receiver_drain_failed"
    finally:
        trial.close()


def test_cw06_targeted_claim_cannot_escape_scope_or_steal_lease(fixture):
    service = fixture.load()
    headers, body = fixture.event()
    journal = service.inbound.journal
    journal.accept_delivery(service.scope, dict(headers)["Linear-Delivery"], body, json.loads(body))
    row = journal.deliveries(service.scope)[0]
    foreign = replace(service.scope, logical_session="other-session")
    assert journal.claim_delivery(foreign, "foreign", time.time(), receipt_id=row["id"]) is None
    assert (
        journal.claim_delivery(service.scope, "missing", time.time(), receipt_id=uid(999)) is None
    )
    claimed = journal.claim_delivery(service.scope, "first", time.time(), receipt_id=row["id"])
    assert claimed["id"] == row["id"]
    assert (
        journal.claim_delivery(service.scope, "second", time.time(), receipt_id=row["id"]) is None
    )


@pytest.mark.parametrize("outcome", ["uncertain", "readback_failure"])
def test_cw07_uncertain_outcome_stays_parked_until_explicit_recovery(fixture, monkeypatch, outcome):
    from ai_task_manager.application.linear_sync.reconcile import Reconciliation

    trial = Trial(fixture, monkeypatch, mode=outcome)
    try:
        original = trial.accept()
        trial.confirm("uncertain")
        before = len(fixture.provider.requests)
        time.sleep(0.8)
        assert len(fixture.provider.requests) == before
        assert trial.rows()[0]["state"] == "received"
        current = trial.service.core.get_task(trial.service.handle, trial.task.id)
        assert current.observed_status == "Backlog" and current.decision_revision == 0
        assert not trial.service.core.external.observations(trial.service.handle, current.id)
        operation = trial.service.core.sync.list_operations(trial.service.handle)[-1]
        assert operation.state == "uncertain"
        recovery = Reconciliation(
            trial.service.core, trial.service.inbound.client, trial.service.inbound.journal
        )
        if outcome == "uncertain":
            from ai_task_manager.adapters.linear.errors import LinearError

            with pytest.raises(LinearError, match="creation_identity_uncertain"):
                recovery.recover_outbound(trial.service.handle, operation.id)
            assert trial.rows()[0]["state"] == "received"
            return
        recovery.recover_outbound(trial.service.handle, operation.id)
        final = wait_for(lambda: next((r for r in trial.rows() if r["state"] == "applied"), None))
        assert final["id"] == original["id"]
        assert trial.service.core.get_task(trial.service.handle, current.id).decision_revision == 0
    finally:
        trial.close()


def test_cw08_foreign_scope_receipt_does_not_drive_recovery(fixture, monkeypatch):
    trial = Trial(fixture, monkeypatch)
    try:
        trial.accept()
        foreign = replace(trial.service.scope, logical_session="foreign-session")
        journal = trial.service.inbound.journal
        journal.accept_delivery(
            foreign, dict(trial.headers)["Linear-Delivery"], trial.body, json.loads(trial.body)
        )
        before = journal.deliveries(foreign)
        trial.confirm()
        wait_for(lambda: trial.rows()[0]["state"] == "applied")
        assert journal.deliveries(foreign) == before
        assert trial.receiver._recovery_delay() is None
    finally:
        trial.close()


def test_cw09_confirmation_during_fresh_readback_still_classifies_echo(fixture, monkeypatch):
    trial = Trial(fixture, monkeypatch)
    read_done, return_read = threading.Event(), threading.Event()
    original = trial.service.inbound.client.read_issue

    def read_then_wait(issue):
        result = original(issue)
        read_done.set()
        assert return_read.wait(8), "Finite authored inbound readback gate"
        return result

    monkeypatch.setattr(trial.service.inbound.client, "read_issue", read_then_wait)
    try:
        headers, body = fixture.event()
        assert request(trial.receiver.http.address, ROUTE, headers, body)[0] == 200
        assert read_done.wait(5)
        trial.confirm()
        return_read.set()
        wait_for(lambda: trial.rows()[0]["state"] == "applied")
        task = trial.service.core.get_task(trial.service.handle, trial.task.id)
        assert task.decision_revision == 0
        assert not trial.service.core.external.observations(trial.service.handle, task.id)
    finally:
        return_read.set()
        trial.close()


def test_cw10_expired_parked_lease_recovers_without_spin_or_provider_poll(fixture, monkeypatch):
    trial = Trial(fixture, monkeypatch)
    try:
        original = trial.accept()
        assert trial.receiver.stop()["stopped"]
        claimed = trial.service.inbound.journal.claim_delivery(
            trial.service.scope,
            "authored-crashed-drain",
            time.time(),
            lease_seconds=0.1,
            receipt_id=original["id"],
        )
        assert json.loads(claimed["checkpoint"])["awaiting_outbound"]
        time.sleep(0.15)
        trial.service = fixture.load()
        trial.receiver = InstalledReceiver(trial.service, fixture.config)
        trial.receiver.start()
        before = len(fixture.provider.requests)
        time.sleep(0.8)
        assert len(fixture.provider.requests) == before
        assert trial.receiver._recovery_delay() == trial.receiver.CONFIRMATION_RECOVERY_SECONDS
        trial.confirm()
        wait_for(lambda: trial.rows()[0]["state"] == "applied")
    finally:
        trial.close()


def test_cw11_newer_prose_is_staged_while_same_status_echo_is_parked(fixture, monkeypatch):
    trial = Trial(fixture, monkeypatch)
    try:
        trial.accept()
        changed_body = "Authored newer human scope requiring independent approval."
        fixture.provider.edit(trial.task.issue_id, description=changed_body)
        headers, body = fixture.event()
        assert request(trial.receiver.http.address, ROUTE, headers, body)[0] == 200
        current = wait_for(
            lambda: (
                task
                if (
                    task := trial.service.core.get_task(trial.service.handle, trial.task.id)
                ).human_hold
                else None
            )
        )
        assert current.body != changed_body
        observations = trial.service.core.external.observations(trial.service.handle, current.id)
        assert any(json.loads(x["payload"])["body"] == changed_body for x in observations)
        assert trial.service.core.sync.list_operations(trial.service.handle)[-1].fenced_by
        trial.confirm("uncertain")
        assert fixture.provider.issues[trial.task.issue_id]["description"] == changed_body
    finally:
        trial.close()


def child_main():
    config, endpoint, task_id, mode = sys.argv[1:]
    assert endpoint.startswith("http://127.0.0.1:")
    components = LinearComponents(
        read_private(config)["linear"],
        client_factory=lambda credentials, expected: LinearClient(
            HttpTransport(_test_endpoint=endpoint), credentials, expected
        ),
    )
    service = load_service(config, linear_components=components)
    outbound = components.outbound(service.core, service.handle, None, None)
    if mode == "crash_response":
        finish = outbound.journal.finish_send

        def finish_then_exit(send_id, state, snapshot=None, error=None):
            finish(send_id, state, snapshot, error)
            if state == "mutation_returned":
                os._exit(71)

        outbound.journal.finish_send = finish_then_exit
    if mode == "crash_unknown":
        update = outbound.client.update_issue

        def update_then_exit(*args):
            update(*args)
            os._exit(72)

        outbound.client.update_issue = update_then_exit
    if mode == "readback_failure":
        from ai_task_manager.adapters.linear.errors import LinearError

        actual_update = outbound.client.update_issue

        def update_then_lose_readback(*args):
            result = actual_update(*args)

            def failed_readback(*_):
                raise LinearError("authored_readback_loss", uncertain=True)

            outbound.client.read_issue = failed_readback
            return result

        outbound.client.update_issue = update_then_lose_readback
    if mode == "uncertain":
        from ai_task_manager.adapters.linear.errors import LinearError

        actual_update = outbound.client.update_issue

        def uncertain(*args):
            actual_update(*args)
            raise LinearError("authored_lost_response", uncertain=True)

        outbound.client.update_issue = uncertain
    if mode == "lost":

        def lost():
            raise OSError("authored lost notification")

        outbound.wake_inbound = lost
    result = outbound.run_once(service.handle, task_id=task_id)
    print(
        json.dumps({"state": result["state"], "pid": os.getpid(), "completed_at": time.monotonic()})
    )


if __name__ == "__main__":
    child_main()
