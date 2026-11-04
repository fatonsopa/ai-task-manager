"""LD13 zero-resource budgets: local evaluator controls, never provider acceptance."""

from dataclasses import replace
from uuid import uuid4

import pytest

from .fixture_control import FixtureControl
from .ledger import KINDS, DriverRefused, Limits, RunLedger, Scope


def recovery_limits():
    # Exact failed recovery packet allowances, with a deterministic local clock.
    return Limits(
        1900.0,
        requests=320,
        projects=1,
        issues=1,
        webhooks=1,
        comments=0,
        started_at=1000.0,
        cleanup_requests=48,
        cleanup_seconds=180,
    )


def record(tmp_path, limits):
    return RunLedger(
        tmp_path / "fixture.sqlite",
        str(uuid4()),
        Scope(*(str(uuid4()) for _ in range(5))),
        limits,
        clock=lambda: 1000.0,
    )


@pytest.mark.parametrize("kind,field", KINDS.items())
def test_zero_resource_refuses_before_reservation_request_or_provider(tmp_path, kind, field):
    ledger = record(tmp_path, replace(recovery_limits(), **{field: 0}))
    calls = []

    class Port:
        def create(self, *args):
            calls.append(args)
            raise AssertionError("Zero allowance delegated a provider mutation")

    candidate = ledger.scope.project if kind == "project" else str(uuid4())
    before = ledger.summary()
    with pytest.raises(DriverRefused, match="^resource_budget_exhausted$"):
        FixtureControl(ledger, Port()).create("forbidden", kind, candidate, "a" * 64)
    assert ledger.summary() == before
    assert not calls
    with pytest.raises(DriverRefused, match="^intent_not_sendable$"):
        ledger.charged_create_send("forbidden")
    assert ledger.summary() == before


@pytest.mark.parametrize("field", KINDS.values())
@pytest.mark.parametrize("value", [-1, True, False, 0.0, 1.5, "0", None, 10001])
def test_invalid_resource_allowances_still_refuse(field, value):
    with pytest.raises(DriverRefused, match="^invalid_limit$"):
        replace(recovery_limits(), **{field: value}).check()


@pytest.mark.parametrize("value", [0, -1, True, False, 1.0, "320", None, 10001])
def test_request_allowance_remains_strictly_positive(value):
    with pytest.raises(DriverRefused, match="^invalid_limit$"):
        replace(recovery_limits(), requests=value).check()


@pytest.mark.parametrize(
    "change",
    [
        {"cleanup_requests": 0},
        {"cleanup_requests": -1},
        {"cleanup_requests": True},
        {"cleanup_requests": 48.0},
        {"cleanup_requests": 320},
        {"cleanup_seconds": 0},
        {"cleanup_seconds": -1},
        {"cleanup_seconds": True},
        {"cleanup_seconds": 900},
        {"cleanup_seconds": float("inf")},
        {"started_at": None},
    ],
)
def test_cleanup_reservation_guards_remain_required(change):
    with pytest.raises(DriverRefused, match="^invalid_cleanup_reserve$"):
        replace(recovery_limits(), **change).check()


@pytest.mark.parametrize("kind,field", KINDS.items())
def test_positive_resource_limit_and_committed_intent_replay_remain_exact(tmp_path, kind, field):
    ledger = record(tmp_path, replace(recovery_limits(), **{field: 1}))
    first = ledger.begin("one", kind, str(uuid4()), "a" * 64)
    assert ledger.begin("one", kind, first["candidate"], "a" * 64) == first
    with pytest.raises(DriverRefused, match="^resource_budget_exhausted$"):
        ledger.begin("two", kind, str(uuid4()), "b" * 64)
    assert len(ledger.summary()["operations"]) == 1


def test_recovery_constructor_preflight_and_durable_zero_binding(tmp_path):
    ledger = record(tmp_path, recovery_limits())
    reopened = RunLedger(
        ledger.path, ledger.run_id, ledger.scope, ledger.limits, clock=lambda: 1000.0
    )
    assert reopened.summary() == ledger.summary()
    assert ledger.limits.comments == 0
    for kind in ("project", "issue", "webhook"):
        ledger.begin(kind, kind, str(uuid4()), "a" * 64)
    with pytest.raises(DriverRefused, match="^resource_budget_exhausted$"):
        ledger.begin("comment", "comment", str(uuid4()), "b" * 64)
    before = ledger.summary()
    with pytest.raises(DriverRefused, match="^ledger_binding_changed$"):
        RunLedger(ledger.path, ledger.run_id, ledger.scope, replace(ledger.limits, comments=1))
    assert ledger.summary() == before


def test_all_optional_resources_can_be_disabled_without_removing_request_reserve(tmp_path):
    ledger = record(tmp_path, replace(recovery_limits(), **dict.fromkeys(KINDS.values(), 0)))
    for _ in range(272):
        ledger.request("read")
    with pytest.raises(DriverRefused, match="^request_budget_exhausted$"):
        ledger.request("read")
    assert len(ledger.summary()["requests"]) == 272
    assert not ledger.summary()["operations"]
