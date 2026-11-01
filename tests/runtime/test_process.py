import subprocess
import sys

import pytest

from ai_task_manager.adapters.process import groups
from ai_task_manager.supervisor.models import RuntimeUncertain


@pytest.mark.parametrize("exitcode,resist", [(0, False), (7, False), (0, True)])
def test_orphaned_group_is_not_confirmed_success_and_foreign_group_survives(rr, exitcode, resist):
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True
    )
    dispatch = rr.start()
    rr.admit(dispatch)
    child = "import time; time.sleep(10)"
    if resist:
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(10)"
    program = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"time.sleep(0.1); raise SystemExit({exitcode})"
    )
    try:
        result = rr.supervisor.execute(
            rr.rig.handle, dispatch, rr.command(dispatch, program), operation_key=rr.rig.key()
        )
        fact = rr.journal.observation(result.observation_id)
        assert result.outcome == "failed" and result.quiescent
        assert fact["completion"]["group"]["orphaned"] is True
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=3)


def test_startup_reply_loss_stops_or_preserves_uncertainty_and_never_relaunches(rr, monkeypatch):
    from ai_task_manager.adapters.process import runtime

    monkeypatch.setattr(runtime.selectors.DefaultSelector, "select", lambda *args, **kwargs: [])
    with pytest.raises(RuntimeUncertain):
        rr.start()
    monkeypatch.undo()
    with rr.journal.transaction() as conn:
        attempt_id = conn.execute("SELECT id FROM runtime_attempts").fetchone()[0]
    assert rr.journal.observations(attempt_id, "startup_failure")[-1]["confirmed_stop"]
    with pytest.raises(RuntimeUncertain):
        rr.runtime.launch_waiting(attempt_id)


def test_process_identity_or_inventory_uncertainty_retains_core_reservation(rr, monkeypatch):
    dispatch = rr.start()
    rr.admit(dispatch)
    unknown = groups.GroupObservation(False, reason="controlled inventory failure")
    monkeypatch.setattr(dispatch.worker.group, "observe", lambda: unknown)
    result = rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    assert result.outcome == "unknown" and not result.quiescent
    assert rr.rig.core.get_attempt(rr.rig.handle, dispatch.attempt_id).state == "unknown"
    task = rr.rig.core.get_task(
        rr.rig.handle, rr.rig.core.get_attempt(rr.rig.handle, dispatch.attempt_id).task_id
    )
    with pytest.raises(Exception):
        rr.rig.core.execution.reserve(
            rr.rig.handle,
            task.id,
            "host",
            task.version,
            task.decision_revision,
            rr.rig.decision(task, "In Progress"),
            rr.rig.key(),
        )
    monkeypatch.undo()


def test_interrupt_preserves_failure_after_actual_group_cleanup(rr, monkeypatch):
    from ai_task_manager.adapters.process import runtime

    dispatch = rr.start()
    rr.admit(dispatch)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(runtime, "supervise", interrupt)
    with pytest.raises(KeyboardInterrupt):
        rr.supervisor.execute(
            rr.rig.handle,
            dispatch,
            rr.command(dispatch, "import time; time.sleep(10)"),
            operation_key=rr.rig.key(),
        )
    assert rr.rig.core.get_attempt(rr.rig.handle, dispatch.attempt_id).state == "failed"
    assert rr.journal.observations(dispatch.attempt_id, "runtime_outcome")[-1]["quiescent"]


def test_bootstrap_and_product_share_group_implementation():
    from tools.build_support import process_group

    assert process_group is groups
    assert process_group.OwnedProcessGroup is groups.OwnedProcessGroup
