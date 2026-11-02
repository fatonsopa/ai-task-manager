"""Local composition controls, with real scoped Core and owned HTTP threads."""

import json
import socket
import threading
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from tests.linear.helpers import WEBHOOK

from .client import send_request
from .ledger import DriverRefused
from .receiver import BoundedReceiver
from .run_primary import PrimaryRun
from .test_actual_adapter import ready
from .test_driver_contract import wait_for

pytest_plugins = ["tests.linear.live.test_actual_adapter", "tests.linear.live.test_driver_contract"]


def configured(rig, record, *, receiver=None, binding=None):
    return PrimaryRun(
        rig.core,
        rig.handle,
        rig.journal,
        binding or rig.receiver.binding,
        WEBHOOK,
        rig.receiver.secret,
        rig.inbound,
        record,
        clock=time.time,
        receiver=receiver,
    )


@pytest.fixture
def owned(adapter, rig):
    ready(adapter)
    record = adapter[2]
    server = BoundedReceiver(object(), record)
    server.start()
    assert server.loop_entered.wait(1)
    runs = []
    try:
        yield rig, record, server, runs
    finally:
        for run in runs:
            assert run.stop(2)["stopped"]
        assert server.stop(2)["stopped"]


def test_pr01_default_primary_run_keeps_original_start_and_stop(adapter, rig):
    run = configured(rig, adapter[2])
    try:
        run.start()
        assert send_request(run.receiver, method="GET", route=run.receiver.health_route)[0] == 200
        assert run.receiver.adapter is run.adapter
        assert run.drain.thread.is_alive()
    finally:
        assert run.stop(2)["stopped"]


@pytest.mark.parametrize("target", ["ledger", "route"])
def test_pr02_wrong_ledger_or_route_preserves_existing_receiver(owned, target, monkeypatch):
    rig, record, server, _ = owned
    placeholder = server.adapter
    if target == "ledger":
        monkeypatch.setattr(server, "ledger", object())
    else:
        server.route += "/wrong"
    with pytest.raises(DriverRefused, match="existing_receiver_not_quiescent"):
        configured(rig, record, receiver=server)
    assert server.adapter is placeholder


def test_pr03_wrong_actual_binding_preserves_placeholder(owned):
    rig, record, server, _ = owned
    placeholder = server.adapter
    binding = replace(
        rig.receiver.binding,
        expected=replace(rig.receiver.binding.expected, project_id=str(uuid4())),
    )
    with pytest.raises(LinearError, match="scope_mismatch"):
        configured(rig, record, receiver=server, binding=binding)
    assert server.adapter is placeholder


def test_pr04_unresolved_http_prevents_rebind(owned):
    rig, record, server, _ = owned
    placeholder = server.adapter
    raw = socket.create_connection(server.address, timeout=1)
    try:
        raw.sendall(b"POST")
        wait_for(lambda: server.active == 1)
        with pytest.raises(DriverRefused, match="existing_receiver_not_quiescent"):
            configured(rig, record, receiver=server)
        assert server.adapter is placeholder
    finally:
        raw.close()


def test_pr05_drain_start_failure_retains_placeholder_and_receiver(owned, monkeypatch):
    rig, record, server, runs = owned
    placeholder = server.adapter
    run = configured(rig, record, receiver=server)
    runs.append(run)

    def failed():
        raise RuntimeError("authored_drain_start_failure")

    monkeypatch.setattr(run.drain, "start", failed)
    with pytest.raises(RuntimeError, match="authored_drain_start_failure"):
        run.start()
    assert server.adapter is placeholder and server.thread.is_alive()
    assert run.drain.thread is None


def test_pr06_receiver_loss_stops_new_drain_and_keeps_placeholder(owned, monkeypatch):
    rig, record, server, runs = owned
    placeholder = server.adapter
    run = configured(rig, record, receiver=server)
    runs.append(run)
    start = run.drain.start

    def interrupted():
        start()
        assert run.drain.thread.is_alive()
        assert server.stop(2)["stopped"]

    monkeypatch.setattr(run.drain, "start", interrupted)
    with pytest.raises(DriverRefused, match="existing_receiver_not_quiescent"):
        run.start()
    assert server.adapter is placeholder and not run.drain.thread.is_alive()


def test_pr07_actual_drain_precedes_publication_without_duplicate_receiver(
    owned, monkeypatch, tmp_path
):
    rig, record, server, runs = owned
    placeholder, thread, address = server.adapter, server.thread, server.address
    run = configured(rig, record, receiver=server)
    runs.append(run)
    start, facts = run.drain.start, []

    def observed_start():
        assert server.adapter is placeholder
        start()
        facts.append(
            {
                "stage": "drain_started",
                "at": time.monotonic(),
                "alive": run.drain.thread.is_alive(),
                "placeholder": server.adapter is placeholder,
            }
        )

    monkeypatch.setattr(run.drain, "start", observed_start)
    assert run.start() == address
    facts.append(
        {
            "stage": "adapter_published",
            "at": time.monotonic(),
            "alive": run.drain.thread.is_alive(),
            "configured": server.adapter is run.adapter,
        }
    )
    assert server.thread is thread and server.address == address
    assert server.product_run is run and all(row["alive"] for row in facts)
    assert facts[0]["placeholder"] and facts[1]["configured"]
    assert facts[0]["at"] <= facts[1]["at"]
    (tmp_path / "actual-rebind-order.json").write_text(json.dumps(facts, indent=2) + "\n")


def test_pr08_duplicate_activation_preserves_working_consumer(owned):
    rig, record, server, runs = owned
    run = configured(rig, record, receiver=server)
    runs.append(run)
    run.start()
    thread, adapter = run.drain.thread, server.adapter
    with pytest.raises(DriverRefused, match="already_activated"):
        run.start()
    assert server.adapter is adapter and run.drain.thread is thread
    assert thread.is_alive() and not run.drain.closing.is_set()


def test_pr09_actual_drain_thread_start_failure_preserves_original_error(owned, monkeypatch):
    rig, record, server, runs = owned
    placeholder = server.adapter
    run = configured(rig, record, receiver=server)
    runs.append(run)
    original = threading.Thread.start

    def fail(thread):
        if thread.name == "owned-linear-drain":
            raise RuntimeError("authored_actual_thread_start")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="authored_actual_thread_start"):
        run.start()
    assert server.adapter is placeholder and server.thread.is_alive()
    assert run.drain.thread.ident is None
    assert run.drain.stop(1)["stopped"]


def test_pr10_different_run_cannot_take_existing_receiver(owned):
    rig, record, server, runs = owned
    first = configured(rig, record, receiver=server)
    second = configured(rig, record, receiver=server)
    runs.append(first)
    first.start()
    adapter, thread = server.adapter, first.drain.thread
    with pytest.raises(DriverRefused, match="existing_receiver_not_quiescent"):
        second.start()
    with pytest.raises(DriverRefused, match="existing_receiver_not_quiescent"):
        configured(rig, record, receiver=server)
    assert second.drain.thread is None and not second.drain.closing.is_set()
    assert server.product_run is first and server.adapter is adapter
    assert first.drain.thread is thread and thread.is_alive()
    assert not first.drain.closing.is_set()
