"""Actual owned groups retain one stop outcome while shutdown cancels observations."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import anyio
import pytest

from ai_task_manager.adapters.hosts.transports.claude import ClaudeTransport
from ai_task_manager.domains.work.errors import AccessDenied


def test_concurrent_transport_closes_stop_and_reap_exact_group_once(native, monkeypatch):
    process, _, _ = native()
    original = process.group.stop
    entered, release, attempted = Event(), Event(), Event()
    calls, lock = [], Lock()

    def held_stop():
        with lock:
            calls.append(True)
        entered.set()
        assert release.wait(2)
        return original()

    monkeypatch.setattr(process.group, "stop", held_stop)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process.stop)
        assert entered.wait(2)

        def concurrent_close():
            attempted.set()
            return process.stop()

        second = pool.submit(concurrent_close)
        assert attempted.wait(2)
        try:
            time.sleep(0.03)
            assert len(calls) == 1
        finally:
            release.set()
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert a is b is process.stop_evidence
    assert a.confirmed and a.observation.known and not a.observation.live_members
    assert len(calls) == 1 and process.process.returncode is not None
    assert process.stop() is a and len(calls) == 1


def test_shutdown_cancels_reader_without_stale_observation_or_write_permission(native):
    process, transcript, _ = native()
    calls = []

    def expired_boundary():
        calls.append(True)
        raise AccessDenied("Authored expired current work authority")

    process.boundary = expired_boundary
    with pytest.raises(AccessDenied, match="expired current"):
        process.read()
    assert len(calls) == 1
    process.begin_shutdown()
    assert process.read() is None and transcript.read() is None
    assert transcript._last is None
    with pytest.raises(AccessDenied, match="stopping"):
        process.write(json.dumps({"op": "echo"}) + "\n")
    with pytest.raises(AccessDenied, match="stopping"), process._input_boundary():
        pytest.fail("Shutdown must not permit a pending effect writer")
    transport = ClaudeTransport(process, transcript)
    transport.connected = True

    async def drain():
        return [frame async for frame in transport.read_messages()]

    assert anyio.run(drain) == []
    assert len(calls) == 1
    assert process.stop().confirmed
