"""Real prepared startup/Core consumption over authored owned local processes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.workflows.member_evidence import MemberMilestones
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_startup_configuration_fence import after_proof


@pytest.mark.parametrize("change", [None, "expiry", "cancel", "source"])
def test_managed_start_serializes_actual_member_proof_consumption_before_core(composition, change):
    value = composition()
    runtime, task, _, workers, factory, provider, _ = value
    original = runtime.rig.core.context.evidence
    runtime.rig.core.context.evidence = MemberMilestones(original.routes, ())
    held, release, checked_event, refused = Event(), Event(), Event(), Event()
    observations, consumed = [], []
    original_check = factory.startup_verifier.check_prepared_host_ready

    def checked(observed):
        checked_event.set()
        consumed.append(observed)
        try:
            return original_check(observed)
        except BaseException:
            refused.set()
            raise

    factory.startup_verifier.check_prepared_host_ready = checked
    with ThreadPoolExecutor(max_workers=2) as pool:

        def concurrent_observation(native):
            def observe():
                with native.startup.model_guard.observation_boundary(not_after=workers.deadline):
                    held.set()
                    assert release.wait(3)

            observations.append(pool.submit(observe))
            assert held.wait(1)

        after_proof(value, concurrent_observation)
        starting = pool.submit(start, value)
        try:
            assert held.wait(2)
            # The actual Core is available while the other thread owns the guard.
            with runtime.rig.db.transaction() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            if refused.wait(0.15):
                release.set()
                starting.result(timeout=3)
            if starting.done():
                starting.result(timeout=1)
            assert consumed == []
            assert provider.calls == []
            native = next(iter(factory.owned.values()))
            if change == "expiry":
                native.journal.clock = lambda: workers.deadline + 1
            elif change == "cancel":
                native.begin_shutdown()
            elif change == "source":
                native.policy.executable.write_text("changed authored executable source")
        finally:
            release.set()
        observations[0].result(timeout=2)
        if change is None:
            result = starting.result(timeout=3)
        else:
            with pytest.raises((AccessDenied, EvidenceRequired)):
                starting.result(timeout=3)
    if change is None:
        assert result.id == next(iter(factory.owned))
        assert len(consumed) == 1
        assert provider.calls == [task.id]
    else:
        assert provider.calls == []
        with runtime.rig.db.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='worker_started'",
                    (task.id,),
                ).fetchone()[0]
                == 0
            )
