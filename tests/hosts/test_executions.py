"""Scoped registry/merge port fixtures; no project script or provider execution."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.executions import (
    ConfinedWork,
    ModelWork,
    ProtectedMergeWork,
    ReviewedExecutions,
)
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain


class FixtureTests:
    def __init__(self):
        self.calls = []

    def execute_registered(self, *args):
        self.calls.append(args)
        return {"kind": "test_fixture"}

    def reconcile_stopped(self, attempt_id):
        return True


class FixtureAccounting:
    def __init__(self):
        self.calls = []

    def authorize(self, reference, attempt):
        self.calls.append(("authorize", reference))
        return "permit"

    def record(self, permit, **data):
        self.calls.append(("record", data))


def call(
    registry,
    reference,
    resources,
    *,
    boundary=lambda: None,
    authority=lambda: None,
    key="operation",
):
    @contextmanager
    def short():
        yield

    return registry.execute(
        handle=None,
        attempt=SimpleNamespace(id="attempt", task_id="task", host="codex"),
        resources=resources,
        host=SimpleNamespace(execute_model_turn=lambda prompt: {"fixture_turn": prompt}),
        command_ref=reference,
        operation_key=key,
        boundary=boundary,
        effect_boundary=short,
        merge_authority=authority,
    )


def test_unknown_wrong_task_and_repeated_invocation_refuse_without_another_effect(journal):
    runner = FixtureTests()
    accounting = FixtureAccounting()
    registry = ReviewedExecutions(
        journal,
        {
            "test": ConfinedWork("task", "registered-test"),
            "foreign": ConfinedWork("other-task", "other"),
        },
        accounting,
        runner,
    )
    for name in ["unknown", "foreign"]:
        with pytest.raises(AccessDenied):
            call(registry, name, None)
    assert not runner.calls
    assert call(registry, "test", None) == {"kind": "test_fixture"}
    with pytest.raises(AccessDenied):
        call(registry, "test", None)
    assert len(runner.calls) == 1 and not accounting.calls


def test_model_invocation_requires_accounting_and_retains_actual_failure(journal):
    accounting = FixtureAccounting()
    runner = FixtureTests()
    registry = ReviewedExecutions(
        journal,
        {"model": ModelWork("task", "codex", "fixture prompt", "accounted")},
        accounting,
        runner,
    )
    result = call(registry, "model", None)
    assert result == {"fixture_turn": "fixture prompt"}
    assert accounting.calls[0] == ("authorize", "accounted")
    assert accounting.calls[-1][1]["outcome"] == "observed_turn"


@pytest.mark.parametrize("remove,warning", [(True, False), (False, True)])
def test_foreground_merge_port_preserves_result_and_script_owned_db_lifecycle(
    journal, tmp_path, remove, warning
):
    database = tmp_path / "fixture.sqlite"
    database.write_text("disposable fixture")
    tests = FixtureTests()
    accounting = FixtureAccounting()
    checks = []

    def before_start():
        assert database.exists()
        checks.append("full-resource-check")

    def current_authority():
        checks.append("coordinator-authority-without-app-db")

    exact_result = {
        "state": "MERGED",
        "commit": "fixture-commit",
        "cleanup": "warning" if warning else "removed",
    }

    class ForegroundFixture:
        calls = []

        def reconcile_stopped(self, attempt_id):
            return True

        def execute_registered(self, reference, context):
            self.calls.append((reference, context.operation_key))
            context.before_start()
            # The fixture represents the existing foreground script's own DB step.
            if remove:
                database.unlink()
            context.check_authority()
            return exact_result

    merge = ForegroundFixture()
    registry = ReviewedExecutions(
        journal, {"merge": ProtectedMergeWork("task", "unchanged-route")}, accounting, tests, merge
    )
    result = call(
        registry,
        "merge",
        SimpleNamespace(database=database),
        boundary=before_start,
        authority=current_authority,
    )
    assert result is exact_result and result["state"] == "MERGED"
    assert merge.calls == [("unchanged-route", "operation")]
    assert checks[-1] == "coordinator-authority-without-app-db"
    assert database.exists() is not remove
    assert not tests.calls and not accounting.calls


@pytest.mark.parametrize("state", ["REFUSED", "UNKNOWN", "INTERRUPTED"])
def test_existing_foreground_result_meanings_are_not_rewritten(journal, state):
    value = {
        "state": state,
        "route": "existing-lead-recovery" if state != "REFUSED" else "no-effects",
    }
    merge = SimpleNamespace(
        reconcile_stopped=lambda _: True, execute_registered=lambda ref, context: value
    )
    registry = ReviewedExecutions(
        journal,
        {"merge": ProtectedMergeWork("task", "route")},
        FixtureAccounting(),
        FixtureTests(),
        merge,
    )
    assert call(registry, "merge", None) is value


def test_unknown_merge_ownership_blocks_new_foreground_invocation(journal):
    calls = []
    merge = SimpleNamespace(
        reconcile_stopped=lambda _: False, execute_registered=lambda *args: calls.append(args)
    )
    registry = ReviewedExecutions(
        journal,
        {"merge": ProtectedMergeWork("task", "route")},
        FixtureAccounting(),
        FixtureTests(),
        merge,
    )
    with pytest.raises(RuntimeUncertain):
        call(registry, "merge", None)
    assert not calls and registry.reconcile_stopped("attempt") is False


def test_missing_merge_adapter_is_an_integration_refusal_not_an_approval_queue(journal):
    tests = FixtureTests()
    registry = ReviewedExecutions(
        journal, {"merge": ProtectedMergeWork("task", "route")}, FixtureAccounting(), tests
    )
    with pytest.raises(AccessDenied, match="not configured"):
        call(registry, "merge", None)
    assert not tests.calls and not journal._intents()


def test_actual_attempted_model_failure_is_recorded_without_erasing_intent(journal):
    accounting = FixtureAccounting()
    registry = ReviewedExecutions(
        journal,
        {"model": ModelWork("task", "codex", "fixture", "accounted")},
        accounting,
        FixtureTests(),
    )

    def fail(prompt):
        raise RuntimeUncertain("Synthetic interrupted invocation")

    @contextmanager
    def short():
        yield

    with pytest.raises(RuntimeUncertain):
        registry.execute(
            handle=None,
            attempt=SimpleNamespace(id="attempt", task_id="task", host="codex"),
            resources=None,
            host=SimpleNamespace(execute_model_turn=fail),
            command_ref="model",
            operation_key="failed",
            boundary=lambda: None,
            effect_boundary=short,
        )
    assert accounting.calls[-1][1]["outcome"] == "failed_or_uncertain"
    assert journal.intent("execution:failed")["attempt_id"] == "attempt"
