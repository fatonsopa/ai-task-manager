"""Typed producer/effect controls through the actual authored production graph."""

from contextlib import contextmanager

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_model_stability import claude_control, fallback
from tools.build_control import ControlError


@pytest.mark.parametrize("purpose", ["initialization", "auth"])
def test_untyped_producer_refuses_before_owned_process(composition, purpose):
    value = composition()
    factory = value[4]
    method = "authorize_initialization" if purpose == "initialization" else "authorize_auth"
    setattr(factory.permits, method, lambda policy, binding: "data-is-not-a-capability")
    with pytest.raises(AccessDenied, match="typed"):
        start(value)
    assert value[5].calls == []
    for native in factory.owned.values():
        assert native.process is None


def test_model_and_file_effect_follow_root_guard_core_order(composition, monkeypatch):
    value = composition()
    runtime, _, _, workers, factory, _, _ = value
    attempt = start(value)
    native = factory.owned[attempt.id]
    accounting = factory.permits.accounting
    order, inside = [], []
    root_launch = accounting.control.launch_boundary
    root_effect = accounting.control.effect_boundary
    current_guard = native.startup.model_guard.current_boundary
    core = runtime.rig.core.execution.work_boundary
    audit = native.process.input_audit.record
    audit_calls = []

    def audit_outside(outcome, call_id):
        assert not inside, "Input audit ran inside Root/model/Core"
        audit_calls.append(call_id)
        return audit(outcome, call_id)

    def wrap(label, original):
        @contextmanager
        def context(*args, **kwargs):
            with original(*args, **kwargs) as result:
                inside.append(label)
                order.append(tuple(inside))
                try:
                    yield result
                finally:
                    assert inside.pop() == label

        return context

    monkeypatch.setattr(accounting.control, "launch_boundary", wrap("Root", root_launch))
    monkeypatch.setattr(accounting.control, "effect_boundary", wrap("Root", root_effect))
    monkeypatch.setattr(
        native.startup.model_guard, "current_boundary", wrap("model", current_guard)
    )
    monkeypatch.setattr(runtime.rig.core.execution, "work_boundary", wrap("Core", core))
    monkeypatch.setattr(native.process.input_audit, "record", audit_outside)
    workers.execute_reviewed(runtime.rig.handle, attempt.id, "assignment", "ordered-assignment")
    assert order.count(("Root", "model", "Core")) >= 2
    assert not inside
    assert audit_calls
    assert (native.pair.worktree / "owned.py").read_text() == "answer = 42\n"


def test_root_pause_between_model_reply_and_tool_write_denies_publication(composition, monkeypatch):
    value = composition()
    runtime, _, _, workers, factory, _, _ = value
    attempt = start(value)
    native = factory.owned[attempt.id]
    intent = native.journal.intend
    reached = []

    def pause(key, binding):
        result = intent(key, binding)
        if key.startswith("edit:"):
            reached.append(True)
            factory.permits.accounting.rig.admission(True)
        return result

    monkeypatch.setattr(native.journal, "intend", pause)
    with pytest.raises((AccessDenied, ControlError, RuntimeUncertain)):
        workers.execute_reviewed(runtime.rig.handle, attempt.id, "assignment", "paused-assignment")
    assert reached == [True]
    assert not (native.pair.worktree / "owned.py").exists()
    assert native.process._closed


@pytest.mark.parametrize("variant", ["unbound", "sticky"])
def test_initialization_guard_cannot_grant_work_proof(native, binding, journal, variant):
    _, _, emit, observer, initialize = claude_control(native, binding, journal)
    guard = observer.model_guard
    if variant == "sticky":
        initialize()
        with pytest.raises(AccessDenied):
            emit(fallback())
        with pytest.raises(AccessDenied):
            guard.prepare_effect(initialization=True)
    else:
        prepared = guard.prepare_effect(initialization=True)
        with guard.current_boundary(prepared):
            assert observer.ready_ref is None
        with pytest.raises(AccessDenied):
            guard.prepare_effect()
        assert observer.ready_ref is None
