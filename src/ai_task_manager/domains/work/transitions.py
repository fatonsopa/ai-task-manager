"""Pure issue policy. Execution admission and provider confirmation are separate."""

from .errors import InvalidTransition
from .models import Cause as C
from .models import IssueStatus as S
from .models import TransitionFacts

_PAIRS = {
    C.ENQUEUE: {(S.BACKLOG, S.TODO), (S.ON_HOLD, S.TODO)},
    C.DEQUEUE: {(S.TODO, S.BACKLOG), (S.ON_HOLD, S.BACKLOG)},
    C.START: {(S.TODO, S.IN_PROGRESS)},
    C.RESUME: {(S.ON_HOLD, S.IN_PROGRESS)},
    C.HOLD: {(S.IN_PROGRESS, S.ON_HOLD)},
    C.REQUEST_REVIEW: {(S.IN_PROGRESS, S.IN_REVIEW)},
    C.MERGE: {(S.IN_PROGRESS, S.IN_DEV)},
    C.STAGE: {(S.IN_DEV, S.IN_STAGING)},
    C.RELEASE: {(S.IN_STAGING, S.DONE)},
    C.NON_CODE_COMPLETE: {(S.IN_PROGRESS, S.DONE)},
    C.REVIEW_DECISION: {(S.IN_REVIEW, target) for target in S},
    C.CANCEL: {
        (source, S.CANCELED)
        for source in (S.BACKLOG, S.TODO, S.IN_PROGRESS, S.ON_HOLD, S.IN_REVIEW)
    },
    C.DUPLICATE: {
        (source, S.DUPLICATE)
        for source in (S.BACKLOG, S.TODO, S.IN_PROGRESS, S.ON_HOLD, S.IN_REVIEW)
    },
}


def check_transition(source: S, target: S, cause: C, facts: TransitionFacts) -> bool:
    """Return whether state changes; reject unknown causes and missing guards."""
    try:
        source, target, cause = S(source), S(target), C(cause)
    except ValueError as exc:
        raise InvalidTransition("Unknown issue status or cause") from exc
    if not facts.authorized:
        raise InvalidTransition("An authorized decision is required")
    if source == target:
        return False
    if (source, target) not in _PAIRS[cause]:
        raise InvalidTransition(f"{cause}: {source} → {target} is forbidden")
    required = {
        S.BACKLOG: facts.quiescent,
        S.TODO: facts.quiescent,
        S.IN_PROGRESS: facts.queued and facts.worker_started,
        S.ON_HOLD: facts.reason and facts.quiescent,
        S.IN_REVIEW: facts.review_requested and facts.quiescent,
        S.IN_DEV: facts.complete and facts.merged and facts.quiescent,
        S.IN_STAGING: facts.complete
        and facts.merged
        and facts.staged
        and facts.quiescent,
        S.DONE: facts.complete
        and facts.quiescent
        and (
            (facts.merged and facts.staged and facts.released)
            if facts.code_bearing
            else facts.non_code_verified
        ),
        S.CANCELED: facts.reason and facts.quiescent,
        S.DUPLICATE: facts.reason and facts.quiescent and facts.duplicate_link,
    }[target]
    if cause == C.NON_CODE_COMPLETE and facts.code_bearing:
        required = False
    if not required:
        raise InvalidTransition("Destination evidence is incomplete")
    return True
