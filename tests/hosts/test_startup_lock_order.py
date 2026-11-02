"""Current startup contention controls through owned fixtures and actual Core stores."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_model_stability import claude_control, fallback
from tests.hosts.test_startup_configuration_fence import after_proof


def test_startup_contention_refuses_without_waiting_or_publication(composition):
    value = composition()
    runtime, task, _, workers, factory, provider, _ = value
    held, waiting_for_core, entered_core = Event(), Event(), Event()
    executor = ThreadPoolExecutor(max_workers=1)
    futures = []
    original = factory.startup_verifier.check_prepared_host_ready
    observed = []

    def contention(native):
        guard = native.startup.model_guard

        def effect():
            with guard._lock:
                held.set()
                assert waiting_for_core.wait(3)
                # Independent connection to the same actual Core database.
                with runtime.rig.db.transaction() as connection:
                    connection.execute("SELECT 1").fetchone()
                    entered_core.set()

        futures.append(executor.submit(effect))
        assert held.wait(3)

    after_proof(value, contention)

    def checked(observation):
        waiting_for_core.set()
        assert held.is_set() and not entered_core.is_set()
        try:
            return original(observation)
        finally:
            observed.append(True)

    factory.startup_verifier.check_prepared_host_ready = checked
    try:
        with pytest.raises((AccessDenied, EvidenceRequired), match="contended"):
            start(value)
        assert observed == [True]
        futures[0].result(timeout=3)
        assert entered_core.is_set()
        guard = next(iter(factory.owned.values())).startup.model_guard
        assert guard.refusal is None
        with runtime.rig.db.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='worker_started'",
                    (task.id,),
                ).fetchone()[0]
                == 0
            )
        assert provider.calls == []
    finally:
        waiting_for_core.set()
        executor.shutdown(wait=True)


def test_startup_metadata_failure_fences_reentrantly_without_io(composition, monkeypatch):
    value = composition()
    markers = []

    def replace_metadata(native):
        def failed(metadata):
            markers.append(True)
            raise AccessDenied("Authored current policy metadata failure")

        monkeypatch.setattr(
            type(native.policy), "check_admission_metadata", lambda self, metadata: failed(metadata)
        )
        append = native.startup.model_guard.journal._append

        def forbidden(*args, **kwargs):
            raise AssertionError("Startup metadata refusal wrote a journal inside Core")

        # The product abort path may log outside Core, so scope this check to
        # the guard's refusal method, whose record=False must remain unchanged.
        refuse = native.startup.model_guard.configuration_changed

        def checked(source, *, record=True):
            assert record is False
            monkeypatch.setattr(native.startup.model_guard.journal, "_append", forbidden)
            try:
                return refuse(source, record=record)
            finally:
                monkeypatch.setattr(native.startup.model_guard.journal, "_append", append)

        monkeypatch.setattr(native.startup.model_guard, "configuration_changed", checked)

    after_proof(value, replace_metadata)
    with pytest.raises((AccessDenied, EvidenceRequired)):
        start(value)
    assert markers == [True]
    native = next(iter(value[4].owned.values()))
    assert native.startup.model_guard.refusal == "native_configuration_continuity_failed"
    assert value[5].calls == []


@pytest.mark.parametrize("variant", ["reentrant", "sticky"])
def test_startup_observation_preserves_reentrant_and_sticky_checks(
    native,
    binding,
    journal,
    variant,
):
    _, _, emit, observer, initialize = claude_control(native, binding, journal)
    initialize()
    guard = observer.model_guard
    if variant == "sticky":
        with pytest.raises(AccessDenied):
            emit(fallback())
        with pytest.raises(AccessDenied):
            with guard.startup_current_boundary():
                pytest.fail("Sticky model fence was bypassed")
    else:
        with guard._lock:
            with guard.startup_current_boundary():
                guard.check_current()


def test_startup_owner_drift_remains_refusing(composition):
    value = composition()
    markers = []

    def changed(native):
        native.session = object()
        markers.append(True)

    after_proof(value, changed)
    with pytest.raises((AccessDenied, EvidenceRequired)):
        start(value)
    assert markers == [True] and value[5].calls == []
