"""Actual owned synthetic native processes; these are not account/model evidence."""

from threading import Event
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_managed_terminal import running_managed as running_managed
from tests.hosts.test_native_boundaries import bridge_fixture


@pytest.mark.parametrize("kind", ["current", "expired", "replaced_policy", "exited"])
def test_owned_native_continuity_preserves_original_session_and_expiry_fence(composition, kind):
    graph = composition()
    attempt = start(graph)
    native = graph[4].owned[attempt.id]
    current = NativeContinuity(native)
    original = native.startup.ready_ref
    original_policy = native.policy
    if kind == "expired":
        current.expires_at = native.journal.clock() - 1
    elif kind == "replaced_policy":
        native.policy = SimpleNamespace()
    elif kind == "exited":
        native.stop()
    if kind != "current":
        try:
            with pytest.raises(AccessDenied):
                current.prepare()
        finally:
            native.policy = original_policy
        return
    prepared, reference = current.prepare()
    current.acknowledge(prepared)
    assert native.startup.ready_ref == original
    record = native.journal._read(reference)
    assert record["payload"]["new_authentication"] is False
    assert record["payload"]["execution_deadline"] == native.binding.deadline
    assert record["expires_at"] <= native.binding.deadline
    current.close()
    with pytest.raises(AccessDenied):
        current.check(prepared)


def test_async_disconnect_is_awaited_once_even_after_repeated_owned_stop(
    running_managed, journal, tmp_path, recwarn
):
    _, _, _, _, bridge, _ = bridge_fixture(running_managed, journal, tmp_path)
    calls = []

    async def close():
        calls.append("awaited")

    bridge.session = SimpleNamespace(close=close)
    assert bridge.stop().confirmed
    assert bridge.stop().confirmed
    assert calls == ["awaited"]
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


class AuthoredDependencyCollection:
    """Explicit process fixture input; never a production provider observation."""

    def __init__(self, core, handle):
        self.core, self.handle = core, handle
        self.binding = core.session_binding(handle)
        self.calls = 0
        self.fail = False
        self.failed = Event()

    def prepare(self, task_id, operation_key, *, expected_version, decision_revision):
        from tests.scheduling.helpers import snapshot

        task = self.core.get_task(self.handle, task_id)
        self.core.context.check(task, expected_version, decision_revision)
        if self.fail:
            self.failed.set()
            raise AccessDenied("Authored dependency collection failure")
        self.calls += 1
        current = snapshot(self.core, self.handle, task, self.core.context.clock())
        return SimpleNamespace(snapshot_ref=self.core.scheduling.sources.issue(current))


@pytest.mark.parametrize("age_before_work", [False, True])
def test_real_managed_composition_refreshes_before_start_and_lease_gated_edit(
    composition, journal, age_before_work
):
    value = composition()
    runtime, task, _, workers, factory, _, _ = value
    core, handle = runtime.rig.core, runtime.rig.handle
    clock = [journal.clock()]
    wall = journal.clock
    core.context.clock = journal.clock = lambda: max(clock[0], wall())
    workers.deadline = clock[0] + 55
    source = AuthoredDependencyCollection(core, handle)
    workers.bind_dependencies(source)
    attempt = start(value)
    authority = workers._freshness[attempt.id]
    refreshed = [
        e
        for e in core.notifications.list_events(handle)
        if e.kind == "scheduling_authority_renewed"
    ]
    assert refreshed
    cap = factory.owned[attempt.id].binding.deadline
    assert all(e.payload["expires_at"] <= cap for e in refreshed)
    current_facts = core.scheduling.explain(handle, task.id)
    assert current_facts["state"] == "ready"
    assert current_facts["source_revision"] >= refreshed[-1].payload["source_revision"]
    if age_before_work:
        clock[0] += 21
    workers.execute_reviewed(handle, attempt.id, "assignment", "fresh-assignment")
    assert (
        workers.dispatches[attempt.id].resources.worktree / "owned.py"
    ).read_text() == "answer = 42\n"
    assert core.get_attempt(handle, attempt.id).state == "done"
    assert factory.owned[attempt.id].process._closed
    assert authority.close()
    assert factory.owned[attempt.id].binding.deadline == cap
    assert source.calls >= (3 if age_before_work else 2)


def test_background_freshness_loss_records_unknown_and_retains_resource_ownership(
    composition, journal
):
    value = composition()
    runtime, _, _, workers, factory, _, _ = value
    core, handle = runtime.rig.core, runtime.rig.handle
    clock = [journal.clock()]
    wall = journal.clock
    core.context.clock = journal.clock = lambda: max(clock[0], wall())
    workers.deadline = clock[0] + 55
    source = AuthoredDependencyCollection(core, handle)
    workers.bind_dependencies(source)
    attempt = start(value)
    authority = workers._freshness[attempt.id]
    source.fail = True
    clock[0] += 21
    assert source.failed.wait(8)
    authority._thread.join(timeout=3)
    assert not authority._thread.is_alive()
    assert factory.owned[attempt.id].process._closed
    assert core.get_attempt(handle, attempt.id).state == "unknown"
    with core.context.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)
            ).fetchone()[0]
            == 2
        )
    assert not (workers.dispatches[attempt.id].resources.worktree / "owned.py").exists()
    stopped = workers.stop(handle, attempt.id, operation_key="recover-freshness-loss")
    assert stopped.state == "paused"
