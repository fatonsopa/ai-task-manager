from dataclasses import replace

import pytest

from ai_task_manager.domains.scheduling.models import Completion, Dependency, DependencySnapshot
from ai_task_manager.domains.scheduling.policy import evaluate, validate
from ai_task_manager.domains.work.errors import ValidationError
from ai_task_manager.domains.work.models import Scope


SCOPE = Scope("linear", "workspace", "principal", "project", "session")


def sample(edges=(), **changes):
    return replace(DependencySnapshot(SCOPE, "target", "a" * 64, "issue", "source",
                   1, 1000.0, 1030.0, "complete", "normal", tuple(edges)), **changes)


@pytest.mark.parametrize("variant", ["missing", "unavailable", "explicit_empty"])
def test_unknown_is_not_empty(variant):
    value = None if variant == "missing" else sample(
        completeness="unavailable" if variant == "unavailable" else "complete"
    )
    result = evaluate(value, {}, 1000.0)
    assert result.state == ("ready" if variant == "explicit_empty" else "unknown")
    assert bool(result.blockers) == (variant != "explicit_empty")


@pytest.mark.parametrize("variant", ["unit_verified", "code_merged", "work_done", "unknown"])
def test_supported_milestones(variant):
    edge = Dependency("predecessor", variant, "first" if variant == "unit_verified" else None)
    if variant == "unknown":
        with pytest.raises(ValidationError):
            validate(sample((edge,)), 1000.0)
        assert evaluate(sample((edge,)), {}, 1000.0).state == "unknown"
        return
    kind = {"unit_verified": "work_unit_complete", "code_merged": "merge",
            "work_done": "production"}[variant]
    fact = Completion("predecessor", variant, "verified", kind,
                      ("first", "second"), ("first", "second"),
                      edge.unit, "commit", "artifact")
    assert evaluate(sample((edge,)), {("predecessor", variant, edge.unit): fact}, 1000).state == "ready"
    assert evaluate(sample((edge,)), {}, 1000).state == "blocked"


@pytest.mark.parametrize("variant", ["failed", "canceled", "duplicate", "missing", "stale", "foreign", "ambiguous"])
def test_negative_predecessor_outcomes(variant):
    edge = Dependency("predecessor", "work_done")
    fact = Completion("predecessor", "work_done", variant, "production",
                      ("complete",), ("complete",), artifact="deployed-looking-artifact")
    result = evaluate(sample((edge,)), {("predecessor", "work_done", None): fact}, 1000)
    assert result.state == "blocked"
    assert result.blockers[0].code == "dependency_" + variant


@pytest.mark.parametrize("variant", ["one_of_two_units", "all_units", "wrong_scope", "wrong_commit"])
def test_partial_work_is_not_whole(variant):
    edge = Dependency("predecessor", "code_merged")
    fact = Completion("foreign" if variant == "wrong_scope" else "predecessor",
                      "code_merged", "verified", "merge", ("first", "second"),
                      ("first", "second") if variant != "one_of_two_units" else ("first",),
                      commit=None if variant == "wrong_commit" else "verified-commit")
    result = evaluate(sample((edge,)), {("predecessor", "code_merged", None): fact}, 1000)
    assert result.state == ("ready" if variant == "all_units" else "blocked")
    unit = Dependency("predecessor", "unit_verified", "first")
    unit_fact = Completion("predecessor", "unit_verified", "verified", "work_unit_complete",
                           ("first", "second"), ("first",), "first")
    assert evaluate(sample((unit,)), {("predecessor", "unit_verified", "first"): unit_fact}, 1000).state == "ready"
