"""Declared local startup failures; no real provider or live-case acceptance."""

import json
import signal
from pathlib import Path

import pytest

from ai_task_manager.application.linear_sync import listener
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.observability.failures import FailureStage
from tests.receiver.helpers import Fixture


@pytest.mark.parametrize("stage", ["load", "configure", "start", "stop"])
def test_startup_diagnostics_preserve_failure_without_private_text(monkeypatch, stage):
    records = []
    secret = "PRIVATE-TOKEN-FOREIGN-PAYLOAD"
    before = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}

    def fail(*_):
        raise AccessDenied(secret)

    class Receiver:
        def __init__(self, *_):
            if stage == "configure":
                fail()

        def start(self):
            fail()

        def stop(self):
            if stage == "stop":
                fail()
            return {"stopped": True}

    monkeypatch.setattr(
        "ai_task_manager.config.service.load_service",
        fail if stage == "load" else lambda *_: object(),
    )
    monkeypatch.setattr(listener, "InstalledReceiver", Receiver)
    assert listener.serve_config(Path("unused"), None, records.append) == 1
    final = records[-1]
    assert final["ok"] is False and final["event"] == "receiver_stopped"
    assert final["cleanup"]["stopped"] is (stage != "stop")
    expected = {
        "load": FailureStage.RECEIVER_LOAD,
        "configure": FailureStage.RECEIVER_CONFIGURE,
        "start": FailureStage.RECEIVER_START,
        "stop": FailureStage.RECEIVER_STOP,
    }[stage]
    assert final["diagnostics"][-1]["stage"] == expected.value
    assert final["diagnostics"][-1]["causes"][0]["code"] == "access_denied"
    assert secret not in json.dumps(records)
    assert all(signal.getsignal(s) == previous for s, previous in before.items())


def test_reconciliation_failure_preserves_origin_without_private_text(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    service = fixture.load()
    receiver = listener.InstalledReceiver(service, fixture.config)
    records = []
    secret = "PRIVATE-RECOVERY-TOKEN-AND-PAYLOAD"

    def fail():
        raise AccessDenied(secret)

    monkeypatch.setattr(receiver, "_reconcile", fail)
    try:
        assert listener.serve(service, fixture.config, records.append, receiver=receiver) == 1
    finally:
        fixture.close()
    final = records[-1]
    assert final["ok"] is False
    assert final["cleanup"]["failure"] == "receiver_reconciliation_failed"
    assert all(
        final["cleanup"][key]
        for key in ("stopped", "drain_stopped", "reconciliation_stopped", "ownership_released")
    )
    assert any(
        item["causes"][0]["code"] == "access_denied" for item in final.get("diagnostics", [])
    ), "The actual reconciliation exception must survive the later shutdown signal"
    assert secret not in json.dumps(records)
