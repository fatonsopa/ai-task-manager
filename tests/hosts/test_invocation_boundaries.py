"""Registered accounting/current-guard controls, not actual model acceptance."""

import os
from dataclasses import replace
from datetime import timedelta

import pytest

from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding, InvocationPermits
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.test_model_stability import claude_control, fallback
from tests.test_build_control_effect_boundaries import running_invocation
from tests.test_build_control_policy_amendment import rig as rig
from tools.build_control import ControlError


def registered(rig):
    controller = running_invocation(rig)
    snapshot = controller.snapshot()
    binding = InvocationBinding.observed(
        {
            "run_id": snapshot["run_id"],
            "policy_revision": snapshot["policy"]["revision"],
            "call": snapshot["calls"][0],
        }
    )
    issuer = InvocationPermits(controller, {"registered": binding})
    return controller, binding, issuer, issuer.prepare("registered")


@pytest.mark.parametrize(
    "field",
    [
        "run",
        "revision",
        "owner",
        "attempt",
        "host",
        "configuration",
        "model",
        "effort",
        "purpose",
        "deadline",
        "call",
    ],
)
def test_registered_invocation_matches_actual_call(rig, field):
    control, binding, _, _ = registered(rig)
    changes = {
        "run": {"run_id": "another-run"},
        "revision": {"policy_revision": 2},
        "owner": {"worker_identity": "another-owner"},
        "attempt": {"attempt_id": "another-attempt"},
        "host": {"host": "claude"},
        "configuration": {"configuration_digest": "b" * 64},
        "model": {"model": "another-fixture-model"},
        "effort": {"effort": "medium"},
        "purpose": {"purpose": "neutral"},
        "deadline": {"deadline": binding.deadline + 1},
        "call": {"call_id": "another-call"},
    }[field]
    issuer = InvocationPermits(control, {"foreign": replace(binding, **changes)})
    with pytest.raises(AccessDenied):
        issuer.prepare("foreign")


@pytest.mark.parametrize("variant", ["missing", "foreign", "rebound"])
def test_unregistered_authority_cannot_enter_effect(rig, monkeypatch, variant):
    control, binding, issuer, prepared = registered(rig)
    if variant == "missing":
        prepared = replace(prepared, issuer=None)
    elif variant == "foreign":
        prepared = replace(prepared, issuer=InvocationPermits(control, {"registered": binding}))
    else:
        monkeypatch.setattr(issuer, "controller", rig.control(1))
    with pytest.raises(AccessDenied):
        with prepared.effect_boundary():
            pytest.fail("Unregistered authority reached an effect")


@pytest.mark.parametrize("variant", ["pause", "deadline", "unknown"])
def test_live_root_refusal_prevents_pipe_effect(rig, variant):
    _, _, _, prepared = registered(rig)
    if variant == "pause":
        rig.admission(True)
    elif variant == "deadline":
        rig.now += timedelta(seconds=31)
    else:
        rig.current.record_outcome(
            "call",
            outcome="unknown",
            process_stopped=False,
            evidence_ref="authored uncertain local fixture",
        )
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    try:
        with pytest.raises(ControlError):
            with prepared.effect_boundary():
                os.write(write_fd, b"forbidden")
        with pytest.raises(BlockingIOError):
            os.read(read_fd, 64)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_persisted_deadline_clamps_consumer_window(rig):
    _, binding, _, prepared = registered(rig)

    def clock():
        return rig.now.timestamp()

    assert prepared.bounded_timeout(100, original_deadline=clock() + 5, clock=clock) == 5
    assert prepared.bounded_timeout(100, original_deadline=clock() + 100, clock=clock) == (
        binding.deadline - clock()
    )
    rig.now += timedelta(seconds=31)
    with pytest.raises(AccessDenied):
        prepared.bounded_timeout(100, original_deadline=clock() + 100, clock=clock)


@pytest.mark.parametrize("variant", ["accepted", "foreign_scope"])
def test_real_pipe_effect_requires_registered_permit(rig, variant):
    _, _, _, prepared = registered(rig)
    if variant == "foreign_scope":
        prepared = replace(prepared, binding=replace(prepared.binding, attempt_id="foreign"))
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    try:
        if variant == "accepted":
            with prepared.effect_boundary():
                assert os.write(write_fd, b"actual-owned-effect") == 19
            assert os.read(read_fd, 64) == b"actual-owned-effect"
        else:
            with pytest.raises(AccessDenied):
                with prepared.effect_boundary():
                    os.write(write_fd, b"forbidden")
            with pytest.raises(BlockingIOError):
                os.read(read_fd, 64)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_prepared_guard_keeps_history_outside_current_boundary(
    rig, native, binding, journal, monkeypatch, tmp_path
):
    _, _, _, observer, initialize = claude_control(native, binding, journal)
    initialize()
    guard = observer.model_guard
    prepared = guard.prepare_effect()
    controller = running_invocation(rig)

    def forbidden(*args, **kwargs):
        raise AssertionError("Durable history read under Root/model locks")

    monkeypatch.setattr(journal, "_history", forbidden)
    monkeypatch.setattr(journal, "_append", forbidden)
    with controller.effect_boundary("call", worker_identity="owner"):
        with guard.current_boundary(prepared):
            (tmp_path / "actual-current-guard-effect").write_bytes(b"published")
    assert (tmp_path / "actual-current-guard-effect").read_bytes() == b"published"


@pytest.mark.parametrize("variant", ["foreign_guard", "replaced_process", "sticky"])
def test_prepared_guard_refuses_owner_or_sticky_drift(
    native, binding, journal, monkeypatch, variant
):
    _, transcript, emit, observer, initialize = claude_control(native, binding, journal)
    initialize()
    guard = observer.model_guard
    prepared = guard.prepare_effect()
    if variant == "foreign_guard":
        _, _, _, other, initialize_other = claude_control(
            native, binding, journal, attempt_id="another-owned-attempt"
        )
        initialize_other()
        guard = other.model_guard
    elif variant == "replaced_process":
        process, _, _ = native()
        monkeypatch.setattr(transcript, "process", process)
    else:
        with pytest.raises(AccessDenied):
            emit(fallback())
    with pytest.raises(AccessDenied):
        with guard.current_boundary(prepared):
            pytest.fail("Changed/fenced model owner reached an effect")


def test_current_guard_uses_only_metadata_port(rig, native, binding, journal):
    _, _, _, observer, initialize = claude_control(native, binding, journal)
    initialize()
    inside, calls = [False], []

    class MetadataPort:
        def check(self):
            assert not inside[0]
            calls.append("full-outside")

        def check_metadata(self):
            assert inside[0]
            calls.append("current-inside")

    guard = observer.model_guard
    guard.configuration = MetadataPort()
    prepared = guard.prepare_effect()
    controller = running_invocation(rig)
    with controller.effect_boundary("call", worker_identity="owner"):
        inside[0] = True
        try:
            with guard.current_boundary(prepared):
                calls.append("bounded-effect")
        finally:
            inside[0] = False
    assert calls == ["full-outside", "current-inside", "bounded-effect"]
