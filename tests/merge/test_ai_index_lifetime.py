from types import SimpleNamespace

import pytest

from ai_task_manager.domains.work.errors import AccessDenied

VARIANTS = ["model_return", "eof", "window", "disconnect"]


@pytest.mark.parametrize("reason", VARIANTS, ids=VARIANTS)
def test_model_end_is_not_protected_operation_stop(harness, reason):
    harness.publish()
    harness.process("active")
    harness.operations.model_ended(harness.ref, harness.version(), "model-end", reason)
    assert harness.operations.reconcile_stopped("attempt") is False
    assert harness.stop.calls == []
    assert "quiescence_evidence" not in harness.current["facts"]
    assert harness.current["facts"]["heavy_obligation"] == "pending"


VARIANTS = ["stopped", "removed", "warning", "unknown"]


@pytest.mark.parametrize("state", VARIANTS, ids=VARIANTS)
def test_expected_application_database_absence(harness, state):
    harness.publish()
    harness.observe("database", {"state": state})
    harness.authority.ready = False
    harness.operations.check_authority(harness.ref)
    assert harness.authority.log.count("before_start") == 1
    assert harness.current["facts"]["database"] == state
    assert harness.current["facts"]["publication"]["new"] == "b" * 40


VARIANTS = [
    "active",
    "unknown",
    "quiescent",
    "missing_child",
    "foreign_child",
    "foreign_parent",
    "incomplete_observation",
    "missing_observer",
]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_observe_stop_is_not_stop_instruction(harness, variant):
    harness.publish()
    changes = {}
    if variant == "missing_child":
        changes["children"] = {}
    elif variant == "foreign_child":
        changes["children"] = {"foreign": "quiescent"}
    elif variant == "foreign_parent":
        changes["process_ref"] = "foreign"
    elif variant == "incomplete_observation":
        changes["complete"] = False
    harness.process(variant if variant in {"active", "unknown"} else "quiescent", **changes)
    if variant == "missing_observer":
        harness.operations.observations = None
    assert harness.operations.reconcile_stopped("attempt") is (variant == "quiescent")
    assert harness.stop.calls == []
    assert ("quiescence_evidence" in harness.current["facts"]) is (variant == "quiescent")
    harness.operations.request_stop(harness.ref, harness.version(), "stop", "authorized-stop")
    assert harness.stop.calls == [("attempt", "authorized-stop")]
    harness.operations.request_stop(harness.ref, harness.version(), "stop", "authorized-stop")
    assert len(harness.stop.calls) == 1


VARIANTS = [
    "valid",
    "missing_authority_boundary",
    "spawn_only",
    "expired",
    "hold_first",
    "overbroad_scope",
]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_single_authority_boundary_and_pending_bound(harness, variant):
    harness.launch()
    harness.prepare()
    harness.observe("database", {"state": "removed"})
    harness.authority.ready = False
    if variant in {"expired", "hold_first"}:
        harness.authority.active = False
        harness.process("unknown")
    elif variant in {"missing_authority_boundary", "spawn_only"}:

        def forbidden_spawn():
            raise AssertionError("Generic spawn guard must not be borrowed")

        harness.operations.authority = SimpleNamespace(
            check_current=harness.authority.check_current, spawn_boundary=forbidden_spawn
        )
    if variant == "valid":
        with harness.operations.authority_boundary(harness.ref):
            assert harness.authority.log[-1] == "authority_enter"
        assert harness.authority.log[-1] == "authority_exit"
    else:
        with pytest.raises(AccessDenied):
            with harness.operations.authority_boundary(
                harness.ref,
                scope="whole_script" if variant == "overbroad_scope" else "final_local_git",
            ):
                raise AssertionError("Unadmitted section entered")
        assert harness.stop.calls == []
        if variant in {"expired", "hold_first"}:
            assert harness.operations.reconcile_stopped("attempt") is False
    assert harness.authority.log.count("before_start") == 1
