"""Real owned-process/Core controls, not native model or provider evidence."""

import sys
import time
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.supervisor.managed import ManagedDispatch, ManagedWorkers
from ai_task_manager.supervisor.models import Command, RuntimeUncertain
from tests.runtime.helpers import RuntimeRig


class FixtureNative:
    def __init__(self, root):
        self.process = HostProcess(
            Command((sys.executable, "-I", "-c", "import time;time.sleep(20)"), root, {}, 20),
            root / "native",
            deadline=time.time() + 20,
        )
        self.process.start()

    def verify_active(self):
        return self.process.verify_identity()

    def stop(self):
        return self.process.stop()


class FixtureExecutions:
    def __init__(self, fail=False, kind="model", known=True):
        self.fail, self.kind, self.known = fail, kind, known

    def execution_kind(self, reference, task_id):
        return self.kind

    def execute(self, **kwargs):
        kwargs["boundary"]()
        if self.fail:
            raise RuntimeUncertain("Synthetic assigned model invocation failure")
        return {"fixture_terminal": True}

    def reconcile_stopped(self, attempt_id):
        return self.known


@pytest.fixture
def running_managed(tmp_path):
    r = RuntimeRig(tmp_path / "runtime")
    dispatch = r.start()
    attempt = r.admit(dispatch)
    native = FixtureNative(tmp_path)
    workers = ManagedWorkers(
        r.rig.core,
        r.journal,
        r.resources,
        r.runtime,
        r.proofs,
        None,
        SimpleNamespace(reconcile_stopped=lambda _: False),
        deadline=r.journal.clock() + 120,
        principal_ref="fixture-principal",
        executions=FixtureExecutions(),
    )
    workers.dispatches[attempt.id] = ManagedDispatch(
        attempt.id, dispatch.resources, dispatch.worker, native
    )
    try:
        yield r, attempt, workers, native
    finally:
        if not native.process._closed:
            native.stop()
        r.close()


def test_terminal_assignment_stops_actual_owned_groups_and_settles_attempt(running_managed):
    r, attempt, workers, native = running_managed
    result = workers.execute_reviewed(r.rig.handle, attempt.id, "registered-assignment", "execute")
    assert result == {"fixture_terminal": True}
    current = r.rig.core.get_attempt(r.rig.handle, attempt.id)
    assert current.state == "done"
    assert native.process._closed
    assert r.rig.core.get_task(r.rig.handle, attempt.task_id).observed_status == "In Progress"


def test_failed_assignment_cannot_leave_running_attempt_or_owned_native_process(running_managed):
    r, attempt, workers, native = running_managed
    workers.executions = FixtureExecutions(fail=True)
    with pytest.raises(RuntimeUncertain):
        workers.execute_reviewed(r.rig.handle, attempt.id, "registered-assignment", "execute")
    assert r.rig.core.get_attempt(r.rig.handle, attempt.id).state == "failed"
    assert native.process._closed


@pytest.mark.parametrize("kind", ["confined", "merge"])
def test_nested_command_keeps_assignment_and_actual_native_group_active(running_managed, kind):
    r, attempt, workers, native = running_managed
    workers.executions = FixtureExecutions(kind=kind)
    assert workers.execute_reviewed(r.rig.handle, attempt.id, "nested", "nested-execution")
    assert r.rig.core.get_attempt(r.rig.handle, attempt.id).state == "running"
    assert native.verify_active().pid > 0


def test_unresolved_command_group_prevents_capacity_release_after_native_stop(running_managed):
    r, attempt, workers, native = running_managed
    workers.executions = FixtureExecutions(known=False)
    with pytest.raises(RuntimeUncertain, match="complete owned stop"):
        workers.execute_reviewed(r.rig.handle, attempt.id, "assignment", "uncertain")
    assert native.process._closed
    assert r.rig.core.get_attempt(r.rig.handle, attempt.id).state == "unknown"
    with r.rig.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)
            ).fetchone()[0]
            == 2
        )


def test_settled_attempt_cannot_launch_again(running_managed):
    from ai_task_manager.domains.work.errors import AccessDenied

    r, attempt, workers, _ = running_managed
    workers.execute_reviewed(r.rig.handle, attempt.id, "assignment", "complete")
    with pytest.raises(AccessDenied, match="already settled"):
        workers.execute_reviewed(r.rig.handle, attempt.id, "assignment", "second")


def test_lost_controller_ignores_waiting_only_stop_and_preserves_owned_resources(running_managed):
    r, attempt, workers, native = running_managed
    original = workers.dispatches.pop(attempt.id)
    # The waiting harness ending proves nothing about the separate native group.
    r.runtime.stop(original.waiting, reason="Synthetic controller loss")
    result = workers.reconcile(r.rig.handle, attempt.id, operation_key="lost-controller")
    assert result.state == "unknown"
    assert native.verify_active().pid > 0
    assert original.resources.worktree.exists() and original.resources.database.exists()
    with pytest.raises(RuntimeUncertain, match="unavailable"):
        workers.execute_reviewed(r.rig.handle, attempt.id, "assignment", "no-relaunch")


def test_restart_recovers_only_complete_managed_stop_before_unfinished_settlement(running_managed):
    r, attempt, workers, _ = running_managed
    dispatch = workers.dispatches[attempt.id]
    real_settle = r.rig.core.execution.settle

    def interrupted(*args, **kwargs):
        raise RuntimeUncertain("Synthetic controller interruption before Core settlement")

    r.rig.core.execution.settle = interrupted
    with pytest.raises(RuntimeUncertain):
        workers.execute_reviewed(r.rig.handle, attempt.id, "assignment", "complete")
    r.rig.core.execution.settle = real_settle
    assert dispatch.host.process._closed
    workers.dispatches.clear()
    recovered = workers.reconcile(r.rig.handle, attempt.id, operation_key="reconnect")
    assert recovered.state == "done"


def test_unconfirmed_native_initialization_stops_before_provider_status_change(tmp_path):
    from ai_task_manager.domains.work.models import CapacityPolicy

    r = RuntimeRig(tmp_path / "runtime")
    proofs = r.rig.core.context.evidence
    r.rig.capacity = CapacityPolicy("codex", 2, 2)
    r.rig.core = r.rig.new_core()
    r.rig.core.context.evidence = proofs
    native = FixtureNative(tmp_path)
    native.initialize = lambda: (_ for _ in ()).throw(
        RuntimeUncertain("Synthetic native initialization refused")
    )
    provider_calls = []
    factory = SimpleNamespace(
        check_admission=lambda _: None,
        configuration_digest=lambda *_: "a" * 64,
        launch=lambda *_: native,
    )
    workers = ManagedWorkers(
        r.rig.core,
        r.journal,
        r.resources,
        r.runtime,
        r.proofs,
        SimpleNamespace(confirm_pending=lambda *args: provider_calls.append(args)),
        factory,
        deadline=r.journal.clock() + 120,
        principal_ref="fixture-principal",
    )
    task = r.rig.queued()
    decision = r.rig.decision(task, "In Progress")
    try:
        with pytest.raises(RuntimeUncertain, match="initialization"):
            workers.start(
                r.rig.handle,
                task.id,
                "codex",
                expected_version=task.version,
                decision_revision=task.decision_revision,
                decision_ref=decision,
                operation_key="failed-start",
            )
        current = r.rig.core.get_task(r.rig.handle, task.id)
        assert current.execution_state == "failed"
        assert current.observed_status == "Todo" and not provider_calls
        assert native.process._closed
        with r.rig.db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM attempts WHERE active=1").fetchone()[0] == 0
    finally:
        if not native.process._closed:
            native.stop()
        r.close()
