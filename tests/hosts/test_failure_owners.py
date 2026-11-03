"""Actual owned authored processes and real Core; never account/model acceptance."""

import json

import pytest

from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.hosts.test_startup_authority import no_started_worker, owned

SECRET = "private-owned-failure-marker"


@pytest.mark.parametrize("retention_fails", [False, True])
def test_authority_first_cause_precedes_cleanup_and_retention_never_masks_original(
    composition,
    monkeypatch,
    retention_fails,
):
    value = composition()
    runtime, task, _, workers, factory, _, _ = value
    core, handle = runtime.rig.core, runtime.rig.handle
    source = AuthoredDependencyCollection(core, handle)
    workers.bind_dependencies(source)
    attempt = start(value)
    native, authority = factory.owned[attempt.id], workers._freshness[attempt.id]
    try:
        Workspace(native.pair.worktree).read("../" + SECRET)
    except AccessDenied as caught:
        original = caught

    def failed(*args, **kwargs):
        raise original

    monkeypatch.setattr(source, "prepare", failed)
    append = native.journal._append

    def persist(kind, payload, **kwargs):
        if retention_fails and payload.get("effect") == "work_authority_failure_cause":
            raise OSError(SECRET)
        return append(kind, payload, **kwargs)

    monkeypatch.setattr(native.journal, "_append", persist)
    with pytest.raises(AccessDenied) as caught:
        authority.refresh()
    assert caught.value is original
    assert core.get_attempt(handle, attempt.id).state == "running"
    assert not native.process._closed
    diagnostic = authority.failure_diagnostics[0]
    assert diagnostic["retained"] is not retention_fails
    assert authority.diagnostic_retention_failed is retention_fails
    assert authority._retention_error is None  # Diagnostics do not invent ownership uncertainty.
    failure = diagnostic["failure"]
    assert failure["stage"] == "authority_refresh"
    assert failure["causes"][0]["code"] == "access_denied"
    assert any(f["module"].endswith("workspace") for f in failure["causes"][0]["frames"])
    assert SECRET not in json.dumps(diagnostic)
    if not retention_fails:
        stored = native.journal._read(diagnostic["reference"], "trace")
        assert stored["payload"]["failure"] == failure
    with core.context.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)
            ).fetchone()[0]
            == 2
        )
    assert workers.stop(handle, attempt.id, operation_key="diagnostic-stop").state == "paused"
    assert native.process._closed


def test_native_turn_refusal_is_persisted_without_a_model_call_or_capacity_release(composition):
    value = composition()
    runtime, _, _, workers, factory, _, _ = value
    attempt = start(value)
    native = factory.owned[attempt.id]
    with pytest.raises(AccessDenied):
        native.execute_model_turn(SECRET, invocation=None)
    diagnostic = native.failure_diagnostics[-1]
    assert diagnostic["retained"]
    assert diagnostic["failure"]["stage"] == "native_turn"
    assert diagnostic["failure"]["causes"][0]["code"] == "access_denied"
    assert SECRET not in json.dumps(diagnostic)
    assert runtime.rig.core.get_attempt(runtime.rig.handle, attempt.id).state == "running"
    assert (
        workers.stop(runtime.rig.handle, attempt.id, operation_key="native-turn-stop").state
        == "paused"
    )


def test_disconnect_error_cause_does_not_replace_confirmed_owned_process_stop(composition):
    value = composition()
    runtime, _, _, workers, factory, _, _ = value
    attempt = start(value)
    native = factory.owned[attempt.id]

    async def close():
        raise OSError(SECRET)

    native.session.close = close
    assert (
        workers.stop(runtime.rig.handle, attempt.id, operation_key="disconnect-stop").state
        == "paused"
    )
    diagnostics = [
        d for d in native.failure_diagnostics if d["failure"]["stage"] == "native_disconnect"
    ]
    assert len(diagnostics) == 1 and diagnostics[0]["retained"]
    assert diagnostics[0]["failure"]["causes"][0]["code"] == "os_error"
    assert SECRET not in json.dumps(diagnostics)
    assert native.process._closed


def test_startup_failure_retains_cause_and_never_claims_worker_readiness(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    provider.disabled = True
    startup.start()
    startup._thread.join(timeout=2)
    assert startup.close()
    diagnostic = startup.failure_diagnostic
    assert diagnostic["retained"] and diagnostic["failure"]["stage"] == "startup_monitor"
    assert type(startup._failure) is AssertionError
    assert diagnostic["failure"]["causes"][0]["code"] == "assertion_error"
    facts = startup.runtime.observations(attempt.id, "startup_failure_cause")
    assert len(facts) == 1 and facts[0]["failure"] == diagnostic["failure"]
    no_started_worker(provider, attempt)
