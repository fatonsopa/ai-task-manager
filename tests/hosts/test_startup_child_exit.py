"""Owned authored child-exit control; no actual native account or model operation."""

from copy import deepcopy

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.hosts.fixture_exit_watch import FixtureExitWatch
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_startup_configuration_fence import after_proof, persist


def test_exited_native_child_with_stopped_live_relay_cannot_start(composition):
    value = composition()
    runtime, task, _, _, factory, provider, _ = value
    observation, timeline, watches = {}, [], []

    def record(stage, payload):
        timeline.append(deepcopy({"stage": stage, **payload}))
        persist("late-native-child-exit-stages", timeline)

    def exit_after_preparation(native):
        process = native.process
        watch = FixtureExitWatch(process, record)
        watches.append(watch)
        watch.stop_parent_and_exit_native()
        assert not process._closed
        assert factory.owned[native.binding.attempt_id] is native
        assert factory.process_registry.get(native.binding.attempt_id) is process
        observation.update(
            native_child_confirmed_nonlive=True,
            relay_bound_watch_not_exited=True,
            relay_stopped=True,
            cached_process_closed=False,
            cached_owner_and_registry_unchanged=True,
        )

    after_proof(value, exit_after_preparation)
    error_name = setup_error = None
    recoveries = []
    try:
        try:
            start(value)
        except (AccessDenied, EvidenceRequired, RuntimeUncertain) as error:
            error_name = type(error).__name__
        except Exception as error:
            setup_error = type(error).__name__ + ": " + str(error)
    finally:
        for watch in watches:
            recoveries.append(watch.recover())
            watch.close()
        with runtime.rig.db.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='worker_started'", (task.id,)
            ).fetchone()[0]
        current = runtime.rig.core.get_task(runtime.rig.handle, task.id)
        observation.update(
            core_worker_started_events=count,
            provider_confirmation_calls=len(provider.calls),
            observed_status=current.observed_status,
            desired_status=current.desired_status,
            error=error_name,
            setup_error=setup_error,
            separate_manual_recoveries=recoveries,
        )
        persist("late-native-child-exit", [observation])
    assert setup_error is None, observation
    assert observation.get("native_child_confirmed_nonlive") is True, observation
    assert recoveries and all(item["confirmed"] for item in recoveries), observation
    assert count == 0 and provider.calls == [], observation
