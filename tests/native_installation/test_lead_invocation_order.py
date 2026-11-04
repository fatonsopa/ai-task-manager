"""Actual case6 Done-event shape through a declared normal lead transport."""

import json
import time

import pytest

from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.installation.native import lead_startup
from ai_task_manager.presentation.cli.main import main
from tests.native_installation import test_lead_startup

lead = test_lead_startup.lead

# Projected payload observed in signed case6 notification input and its Core event.
DONE_PAYLOAD = {"decision_revision": 0, "observed_status": "Done", "task_version": 12}


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
def test_LO01_done_input_survives_concurrent_relay_invocation_admission(lead, monkeypatch, capsys):
    p, b, store, manifest, service, event = lead
    # Declared local consumer data only: no original authority, store or proof copied.
    with service.core.context.db.transaction() as conn:
        conn.execute(
            "UPDATE events SET kind=?,payload=? WHERE id=?",
            ("provider_status_confirmed", json.dumps(DONE_PAYLOAD), event.id),
        )
    original_owner = lead_startup.LeadAccounting._owner
    observations = []
    delayed = []
    original_control = HostProcess._control
    original_ready = getattr(HostProcess, "_invocation_ready", None)

    def flush_control():
        if delayed:
            process, data = delayed.pop()
            original_control(process, data)

    def control(self, data):
        if data.get("op") == "invocation" and data["call_id"].endswith("assignment:lead-event"):
            delayed.append((self, data))
            return None
        return original_control(self, data)

    def ready(self, *args):
        # The repaired protocol must await readiness BEFORE acquiring the writer.
        flush_control()
        return original_ready(self, *args)

    monkeypatch.setattr(HostProcess, "_control", control)
    if original_ready is not None:
        monkeypatch.setattr(HostProcess, "_invocation_ready", ready)

    def bounded_physical_validation(self, policy, binding):
        active = getattr(self.admissions.store._active, "connection", None)
        if active is not None and not observations:
            observations.append("owned_input_writer")
            flush_control()
            # Force the real child relay's 0.1s competing admission to overlap.
            # This is scheduling control, not an authority or model response.
            time.sleep(0.35)
        return original_owner(self, policy, binding)

    monkeypatch.setattr(lead_startup.LeadAccounting, "_owner", bounded_physical_validation)
    code = main(
        [
            "--config",
            str(p.config),
            "--installation",
            str(b.installation),
            "--json",
            "lead",
            "report",
        ]
    )
    captured = capsys.readouterr()
    assert observations == ["owned_input_writer"]
    assert code == 0, captured.err
    with service.core.context.db.transaction() as conn:
        stages = {
            r[0] for r in conn.execute("SELECT stage FROM receipts WHERE event_id=?", (event.id,))
        }
    assert stages == {"queued", "delivered", "ai_acknowledged", "user_reported"}


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
@pytest.mark.parametrize(
    "fault", ["nonce", "request_id", "call_id", "deadline", "missing", "stopped"]
)
def test_LO02_bad_readiness_never_submits_event_input(lead, monkeypatch, capsys, fault):
    p, b, store, manifest, service, event = lead
    original_ready = HostProcess._invocation_ready
    observed = []

    def ready(self, permit, request_id):
        if not permit.binding.call_id.endswith("assignment:lead-event"):
            return original_ready(self, permit, request_id)
        observed.append(fault)
        if fault == "stopped":
            assert self.stop().confirmed
            return original_ready(self, permit, request_id)
        import os

        original_read = os.read
        fd = self._relay_ready_fd

        def read(owned_fd, size):
            raw = original_read(owned_fd, size)
            if owned_fd == fd and raw:
                if fault == "missing":
                    return b""
                data = json.loads(raw)
                data[fault] = data[fault] + 1 if fault == "deadline" else "foreign-or-stale"
                return (json.dumps(data) + "\n").encode()
            return raw

        with monkeypatch.context() as changed:
            changed.setattr(os, "read", read)
            return original_ready(self, permit, request_id)

    monkeypatch.setattr(HostProcess, "_invocation_ready", ready)
    code = main(
        [
            "--config",
            str(p.config),
            "--installation",
            str(b.installation),
            "--json",
            "lead",
            "report",
        ]
    )
    capsys.readouterr()
    assert observed == [fault] and code != 0
    with service.core.context.db.transaction() as conn:
        assert [
            r[0] for r in conn.execute("SELECT stage FROM receipts WHERE event_id=?", (event.id,))
        ] == ["queued"]
    records = [
        json.loads(line)
        for line in (b.root / "process/native-input.jsonl").read_text().splitlines()
    ]
    assert not any(row["call_id"].endswith("assignment:lead-event") for row in records)
    from ai_task_manager.installation.native.controller import NormalInvocationController

    calls = NormalInvocationController(
        store, store.permission(manifest.data["authorization_ref"])
    ).snapshot()["calls"]
    assert all(row["process_stopped"] for row in calls)


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
def test_LO03_current_identity_observation_does_not_consume_readiness(lead, monkeypatch, capsys):
    from concurrent.futures import ThreadPoolExecutor

    p, b, store, manifest, service, event = lead
    original_ready = HostProcess._invocation_ready
    observations = []

    def ready(self, permit, request_id):
        if not permit.binding.call_id.endswith("assignment:lead-event"):
            return original_ready(self, permit, request_id)
        with ThreadPoolExecutor(max_workers=1) as executor:
            current = executor.submit(self.verify_identity)
            result = original_ready(self, permit, request_id)
            observations.append(current.result(timeout=2))
            return result

    monkeypatch.setattr(HostProcess, "_invocation_ready", ready)
    code = main(
        [
            "--config",
            str(p.config),
            "--installation",
            str(b.installation),
            "--json",
            "lead",
            "report",
        ]
    )
    output = capsys.readouterr()
    assert code == 0, output.err
    assert len(observations) == 1


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
def test_LO04_stop_interrupts_wait_without_event_submission(lead, monkeypatch, capsys):
    import selectors
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    p, b, store, manifest, service, event = lead
    original_control = HostProcess._control
    original_ready = HostProcess._invocation_ready
    original_selector = selectors.DefaultSelector
    stopped = []

    def control(self, data):
        if data.get("op") == "invocation" and data["call_id"].endswith("assignment:lead-event"):
            return None  # Declared unavailable relay admission; no acknowledgement invented.
        return original_control(self, data)

    def ready(self, permit, request_id):
        if not permit.binding.call_id.endswith("assignment:lead-event"):
            return original_ready(self, permit, request_id)
        waiting = Event()
        fd = self._relay_ready_fd

        class ObservedSelector(original_selector):
            def select(self, timeout=None):
                if fd in self.get_map():
                    waiting.set()
                return super().select(timeout)

        def cancel():
            assert waiting.wait(2)
            stopped.append(self.stop().confirmed)

        with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as changed:
            changed.setattr(selectors, "DefaultSelector", ObservedSelector)
            future = executor.submit(cancel)
            try:
                return original_ready(self, permit, request_id)
            finally:
                future.result(timeout=2)

    monkeypatch.setattr(HostProcess, "_control", control)
    monkeypatch.setattr(HostProcess, "_invocation_ready", ready)
    code = main(
        [
            "--config",
            str(p.config),
            "--installation",
            str(b.installation),
            "--json",
            "lead",
            "report",
        ]
    )
    capsys.readouterr()
    assert code != 0 and stopped == [True]
    records = [
        json.loads(line)
        for line in (b.root / "process/native-input.jsonl").read_text().splitlines()
    ]
    assert not any(row["call_id"].endswith("assignment:lead-event") for row in records)
    with service.core.context.db.transaction() as conn:
        assert [
            r[0] for r in conn.execute("SELECT stage FROM receipts WHERE event_id=?", (event.id,))
        ] == ["queued"]
