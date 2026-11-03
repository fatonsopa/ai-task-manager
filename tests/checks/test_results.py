from dataclasses import replace

import pytest

from ai_task_manager.domains.checks.results import aggregate, judge
from ai_task_manager.domains.work.errors import ValidationError
from tests.checks.fixtures import execution, plan, sha


@pytest.mark.parametrize(
    "mutation", ["extra", "duplicate", "overlap", "wrong_plan", "wrong_dispatch"]
)
def test_execution_scope(mutation):
    p = plan()
    report = execution(p)
    if mutation == "extra":
        report = replace(report, ran=("foreign",))
    elif mutation == "duplicate":
        report = replace(report, ran=("a", "a"))
    elif mutation == "overlap":
        report = replace(report, failures=(("a", sha("assertion")),))
    elif mutation == "wrong_plan":
        report = replace(report, plan_digest=sha("other plan"))
    else:
        report = replace(report, dispatched=())
    with pytest.raises(ValidationError):
        judge(p, "checks a", ("a",), report)


@pytest.mark.parametrize(
    "cause,summary,code,signal,expected",
    [
        ("completed", True, 0, None, "passed"),
        ("completed", False, 0, None, "missing_summary"),
        ("completed", True, 2, None, "unknown"),
        ("timeout", True, -15, None, "timeout"),
        ("signal", True, -9, 9, "signal"),
        ("setup_failed", True, 1, None, "setup_failed"),
        ("capacity", True, None, None, "capacity"),
        ("unknown", False, None, None, "unknown"),
        ("source_error", True, 1, None, "source_error"),
    ],
)
def test_outcome_categories(cause, summary, code, signal, expected):
    p = plan()
    result = judge(
        p,
        "checks a",
        ("a",),
        execution(p, cause=cause, summary_available=summary, exit_code=code, signal=signal),
    )
    assert result["outcome"] == expected
    assert result["ran"] == ["a"]
    assert result["passed"] == (["a"] if expected == "passed" else [])


def test_named_failure_comparison():
    p = plan()
    failure = (("a", sha("exact assertion signature")),)
    report = execution(p, passed=(), failures=failure, exit_code=1)
    assert judge(p, "checks a", ("a",), report)["outcome"] == "assertion_failed"
    assert judge(p, "checks a", ("a",), report, baseline=failure)["outcome"] == "dev_red"
    assert (
        judge(p, "checks a", ("a",), report, baseline=(("a", sha("other assertion")),))["outcome"]
        == "assertion_failed"
    )
    source_error = replace(report, cause="source_error")
    assert judge(p, "checks a", ("a",), source_error, baseline=failure)["outcome"] == "source_error"


def test_omitted_execution_is_not_a_pass():
    p = plan()
    omitted = execution(p, ran=(), passed=())
    assert judge(p, "checks a", ("a",), omitted)["outcome"] == "unknown"


def test_attribution():
    assert aggregate([{"outcome": "dev_red"}, {"outcome": "source_error"}]) == "failed"
    assert aggregate([{"outcome": "dev_red"}, {"outcome": "timeout"}]) == "blocked"
    assert aggregate([{"outcome": "dev_red"}, {"outcome": "passed"}]) == "dev_red"
    assert aggregate([]) == "passed"


def test_comparison_inputs_are_retained():
    p = plan()
    signature = (("a", sha("exact signature")),)
    report = execution(p, passed=(), failures=signature, exit_code=1)
    absent = judge(p, "checks a", ("a",), report)
    known_empty = judge(p, "checks a", ("a",), report, baseline=())
    matched = judge(p, "checks a", ("a",), report, baseline=signature)
    assert absent["baseline_available"] is False and absent["baseline_failures"] is None
    assert known_empty["baseline_available"] is True and known_empty["baseline_failures"] == []
    assert matched["baseline_failures"] == list(signature)
    assert matched["outcome"] == "dev_red"


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary_available", "yes"),
        ("exit_code", True),
        ("signal", True),
        ("started", float("nan")),
        ("ended", 1.0),
    ],
)
def test_malformed_observation(field, value):
    p = plan()
    with pytest.raises(ValidationError):
        judge(p, "checks a", ("a",), replace(execution(p), **{field: value}))


def test_performance_facts_are_not_benchmark():
    p = plan()
    result = judge(p, "checks a", ("a",), execution(p))
    assert result["active_seconds"] == 1.0
    assert "throughput" not in result and "worker_limit" not in result
    # The1/2/4worker workload, actual hooks and application database costs remain unrun.
