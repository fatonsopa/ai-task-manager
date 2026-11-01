from dataclasses import fields, replace
from itertools import product

import pytest

from ai_task_manager.domains.work.errors import InvalidTransition
from ai_task_manager.domains.work.models import Cause, IssueStatus, TransitionFacts
from ai_task_manager.domains.work.transitions import check_transition

# The acceptance oracle is declared independently of production policy tables.
PAIRS = {
    "enqueue": {("Backlog", "Todo"), ("On Hold", "Todo")},
    "dequeue": {("Todo", "Backlog"), ("On Hold", "Backlog")},
    "start": {("Todo", "In Progress")},
    "resume": {("On Hold", "In Progress")},
    "hold": {("In Progress", "On Hold")},
    "request_review": {("In Progress", "In Review")},
    "merge": {("In Progress", "In Dev")},
    "stage": {("In Dev", "In Staging")},
    "release": {("In Staging", "Done")},
    "non_code_complete": {("In Progress", "Done")},
    "review_decision": {("In Review", status.value) for status in IssueStatus},
    "cancel": {
        (status, "Canceled")
        for status in ("Backlog", "Todo", "In Progress", "On Hold", "In Review")
    },
    "duplicate": {
        (status, "Duplicate")
        for status in ("Backlog", "Todo", "In Progress", "On Hold", "In Review")
    },
}
FULL = TransitionFacts(**{f.name: True for f in fields(TransitionFacts)})


@pytest.mark.parametrize(
    "source,target,cause", list(product(IssueStatus, IssueStatus, Cause))
)
def test_p01_all_ordered_status_pairs_and_causes(source, target, cause):
    facts = replace(FULL, code_bearing=cause != Cause.NON_CODE_COMPLETE)
    allowed = source == target or (source.value, target.value) in PAIRS[cause.value]
    if allowed:
        assert check_transition(source, target, cause, facts) is (source != target)
    else:
        with pytest.raises(InvalidTransition):
            check_transition(source, target, cause, facts)


@pytest.mark.parametrize(
    "target,guards",
    [
        ("Backlog", ["quiescent"]),
        ("Todo", ["quiescent"]),
        ("In Progress", ["queued", "worker_started"]),
        ("On Hold", ["reason", "quiescent"]),
        ("In Dev", ["complete", "merged", "quiescent"]),
        ("In Staging", ["complete", "merged", "staged", "quiescent"]),
        ("Done", ["complete", "merged", "staged", "released", "quiescent"]),
        ("Canceled", ["reason", "quiescent"]),
        ("Duplicate", ["reason", "quiescent", "duplicate_link"]),
    ],
)
def test_p02_review_destinations_have_independent_missing_guard_checks(target, guards):
    assert check_transition("In Review", target, "review_decision", FULL)
    for guard in ["authorized", *guards]:
        with pytest.raises(InvalidTransition):
            check_transition(
                "In Review", target, "review_decision", replace(FULL, **{guard: False})
            )


def test_p02_review_same_state_requires_authority_and_remains_noop():
    assert check_transition("In Review", "In Review", "review_decision", FULL) is False
    with pytest.raises(InvalidTransition):
        check_transition("In Review", "In Review", "review_decision", TransitionFacts())


@pytest.mark.parametrize(
    "source,target,cause",
    [
        ("unknown", "Todo", "enqueue"),
        ("Todo", "unknown", "start"),
        ("Todo", "In Progress", "guess"),
    ],
)
def test_p01_unknown_states_never_become_idle(source, target, cause):
    with pytest.raises(InvalidTransition):
        check_transition(source, target, cause, FULL)


def test_p06_code_cannot_use_non_code_completion():
    with pytest.raises(InvalidTransition):
        check_transition("In Progress", "Done", "non_code_complete", FULL)
    assert check_transition(
        "In Progress", "Done", "non_code_complete", replace(FULL, code_bearing=False)
    )
