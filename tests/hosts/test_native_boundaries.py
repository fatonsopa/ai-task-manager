"""Real local process boundaries; no native model initialization or auth."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.records import HostBinding, ManagedProcesses
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.hosts.test_managed_terminal import running_managed as running_managed


class FixtureStableGuard:
    """Explicit synthetic control for process API tests, not native model evidence."""

    def __init__(self, binding):
        self.binding = binding

    def check(self):
        return None

    @contextmanager
    def effect_boundary(self):
        yield


def bridge_fixture(running_managed, journal, tmp_path):
    r, attempt, workers, fixture = running_managed
    sid, scope = r.rig.core.session_binding(r.rig.handle)
    binding = HostBinding(
        scope,
        sid,
        attempt.id,
        attempt.worker_id,
        "codex",
        "fixture-principal",
        "a" * 64,
        journal.clock() + 30,
    )
    registry = ManagedProcesses()
    registry.attach(attempt.id, fixture.process)
    outcomes = []
    bridge = ManagedNativeHost(
        binding,
        workers.dispatches[attempt.id].resources,
        SimpleNamespace(check=lambda: None, codex_settings=None),
        tmp_path / "bridge",
        journal,
        r.rig.core,
        r.resources,
        SimpleNamespace(record_host_stop=lambda *args: outcomes.append(args)),
        "fixture-account",
        commands=workers,
        process_registry=registry,
    )
    bridge.process = fixture.process
    guard = FixtureStableGuard(binding)
    bridge.startup = SimpleNamespace(admitted=False, model_guard=guard, assert_stable=guard.check)
    return r, attempt, workers, fixture, bridge, outcomes


def test_real_gateway_admission_uses_registered_native_identity(running_managed, journal, tmp_path):
    r, attempt, workers, fixture, bridge, _ = bridge_fixture(running_managed, journal, tmp_path)
    bridge.admit(r.rig.handle, workers.dispatches[attempt.id], attempt)
    assert bridge.gateway.delegate.check().id == attempt.id
    fixture.stop()
    with pytest.raises((AccessDenied, RuntimeUncertain)):
        bridge.gateway.call(
            "write_file",
            {
                "path": "after-stop.py",
                "text": "forbidden",
                "expected_hash": None,
                "operation_key": "after-stop",
            },
        )
    assert not (bridge.pair.worktree / "after-stop.py").exists()


def test_disconnect_failure_still_stops_the_actual_owned_native_group(
    running_managed, journal, tmp_path
):
    _, _, _, fixture, bridge, outcomes = bridge_fixture(running_managed, journal, tmp_path)

    def failed_close():
        raise RuntimeUncertain("Synthetic protocol disconnect failure")

    bridge.session = SimpleNamespace(close=failed_close)
    stopped = bridge.stop()
    assert stopped.confirmed and fixture.process._closed and outcomes
    assert any(
        r["payload"].get("effect") == "native_disconnect_error" for r in journal._history("trace")
    )


def test_worker_command_route_refuses_recursive_assigned_model_invocation(running_managed):
    r, attempt, workers, fixture = running_managed
    with pytest.raises(AccessDenied, match="worker command"):
        workers.execute_worker_command(r.rig.handle, attempt.id, "assignment", "recursive")
    assert fixture.verify_active().pid > 0
    assert r.rig.core.get_attempt(r.rig.handle, attempt.id).state == "running"
