"""Reconcile execution scope and distinguish assertion failures from infrastructure."""

from dataclasses import dataclass
from math import isfinite

from ai_task_manager.domains.work.errors import ValidationError

from .models import Outcome, content_hash, identifier


@dataclass(frozen=True)
class Execution:
    plan_digest: str
    group: str
    dispatched: tuple[str, ...]
    ran: tuple[str, ...]
    passed: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]
    exit_code: int | None
    cause: str
    summary_available: bool
    process_ref: str
    output_digest: str
    started: float
    ended: float
    signal: int | None = None


def comparison(value):
    if value is None:
        return None
    if type(value) is not tuple or any(type(v) is not tuple or len(v) != 2 for v in value):
        raise ValidationError("Comparable baseline signatures must be immutable pairs")
    for name, signature in value:
        identifier(name)
        content_hash(signature)
    if len(set(value)) != len(value):
        raise ValidationError("Comparable baseline signatures must be unique")
    return value


def judge(plan, group, dispatched, execution, *, baseline=None):
    if type(execution) is not Execution:
        raise ValidationError("Configured executor must return an Execution observation")
    if (
        execution.plan_digest != plan.digest
        or execution.group != group
        or execution.dispatched != tuple(dispatched)
    ):
        raise ValidationError("Execution does not match its durable dispatch intent")
    identifier(execution.process_ref)
    content_hash(execution.output_digest)
    if (
        type(execution.summary_available) is not bool
        or (execution.exit_code is not None and type(execution.exit_code) is not int)
        or (
            execution.signal is not None
            and (type(execution.signal) is not int or execution.signal < 1)
        )
    ):
        raise ValidationError("Execution observation contains malformed status fields")
    if (
        not all(
            type(t) in {int, float} and isfinite(t) for t in (execution.started, execution.ended)
        )
        or execution.ended < execution.started
    ):
        raise ValidationError("Execution duration must be finite and ordered")
    ran, passed, failed = set(execution.ran), set(execution.passed), dict(execution.failures)
    if (
        len(ran) != len(execution.ran)
        or len(passed) != len(execution.passed)
        or len(failed) != len(execution.failures)
        or not ran <= set(dispatched)
        or not passed <= ran
        or not set(failed) <= ran
        or passed.intersection(failed)
    ):
        raise ValidationError("Execution scope contains duplicates or unplanned work")
    for name, failure in execution.failures:
        identifier(name)
        content_hash(failure)
    baseline = comparison(baseline)
    if execution.cause == "timeout":
        outcome = Outcome.TIMEOUT
    elif execution.signal is not None or execution.cause == "signal":
        outcome = Outcome.SIGNAL
    elif execution.cause in {"capacity", "setup_failed", "source_error", "unknown"}:
        outcome = Outcome(execution.cause)
    elif execution.cause != "completed":
        raise ValidationError("Unknown execution cause")
    elif not execution.summary_available:
        outcome = Outcome.MISSING
    elif ran != set(dispatched) or passed | set(failed) != ran:
        outcome = Outcome.UNKNOWN
    elif failed and execution.exit_code not in {None, 0}:
        # Only exact named comparable assertion signatures may be attributed to dev.
        outcome = (
            Outcome.DEV_RED
            if baseline is not None and set(execution.failures) <= set(baseline)
            else Outcome.ASSERTION
        )
    elif not failed and execution.exit_code == 0:
        outcome = Outcome.PASSED
    else:
        outcome = Outcome.UNKNOWN
    reusable_passes = (
        sorted(passed) if outcome in {Outcome.PASSED, Outcome.ASSERTION, Outcome.DEV_RED} else []
    )
    return {
        "outcome": outcome.value,
        "ran": sorted(ran),
        "passed": reusable_passes,
        "failures": sorted(execution.failures),
        "baseline_available": baseline is not None,
        "baseline_failures": sorted(baseline) if baseline is not None else None,
        "exit_code": execution.exit_code,
        "signal": execution.signal,
        "cause": execution.cause,
        "summary_available": execution.summary_available,
        "process_ref": execution.process_ref,
        "output_digest": execution.output_digest,
        "started": execution.started,
        "ended": execution.ended,
        "active_seconds": execution.ended - execution.started,
    }


def aggregate(results):
    outcomes = {r["outcome"] for r in results}
    if outcomes.intersection({Outcome.ASSERTION, Outcome.SOURCE}):
        return "failed"
    if outcomes - {Outcome.PASSED, Outcome.DEV_RED, Outcome.SKIPPED}:
        return "blocked"
    return "dev_red" if Outcome.DEV_RED in outcomes else "passed"
