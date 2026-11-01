"""Conservative eligibility from attributable, complete provider state spans."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import WorkError
from .models import DELIVERED, IssueStatus


class ExistingBlocked(WorkError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Missing timezone")
    return result.astimezone(UTC)


@dataclass(frozen=True)
class Assessment:
    digest: str
    reasons: tuple[str, ...]
    delivered: bool
    terminal: bool
    initial_backlog: bool

    @property
    def eligible(self):
        return not self.reasons and not self.delivered and not self.terminal


def assess(snapshot):
    """Absence of activity is never negative evidence of delivery or ownership."""
    reasons, statuses = [], []
    delivered = snapshot.get("status") in DELIVERED
    terminal = snapshot.get("status") in {IssueStatus.CANCELED, IssueStatus.DUPLICATE}
    initial = False
    try:
        spans = snapshot["history"]
        created = stamp(snapshot["created_at"])
        observed = stamp(snapshot["source_revision"])
        if not isinstance(spans, list) or not spans or len(spans) > 5000:
            raise ValueError("Missing or oversized state history")
        seen, previous_end = set(), None
        ordered = sorted(spans, key=lambda item: stamp(item["started_at"]))
        for index, span in enumerate(ordered):
            span_id = span["id"]
            if not isinstance(span_id, str) or not 1 <= len(span_id) <= 256 or span_id in seen:
                raise ValueError("Ambiguous span identity")
            seen.add(span_id)
            start = stamp(span["started_at"])
            end = stamp(span["ended_at"]) if span.get("ended_at") is not None else None
            if start < created or start > observed or (end is not None and end <= start):
                reasons.append("invalid_span_interval")
            if index == 0 and start != created:
                reasons.append("creation_prefix_missing")
            if index and previous_end != start:
                reasons.append("history_gap_or_overlap")
            if index < len(ordered) - 1 and end is None:
                reasons.append("multiple_open_spans")
            state = span.get("state")
            classified = (
                isinstance(state, dict)
                and state.get("id") == span["state_id"]
                and state.get("team_id") == snapshot["team_id"]
                and state.get("name") in set(IssueStatus)
                and stamp(state["created_at"]) <= start
                and stamp(state["updated_at"]) <= start
                and state.get("type")
                in {"backlog", "unstarted", "started", "completed", "canceled", "duplicate"}
            )
            if classified and (
                (state["type"] == "completed") != (state["name"] == IssueStatus.DONE)
                or (state["type"] in {"canceled", "duplicate"})
                != (state["name"] in {IssueStatus.CANCELED, IssueStatus.DUPLICATE})
            ):
                classified = False
            if not classified:
                reasons.append("historic_state_classification_unknown")
                statuses.append(None)
            else:
                status = state["name"]
                statuses.append(status)
                delivered |= status in DELIVERED
                terminal |= status in {IssueStatus.CANCELED, IssueStatus.DUPLICATE}
            previous_end = end
        initial = statuses[0] == IssueStatus.BACKLOG and stamp(ordered[0]["started_at"]) == created
        if not initial:
            reasons.append("initial_backlog_unconfirmed")
        last = ordered[-1]
        if (
            last.get("ended_at") is not None
            or last["state_id"] != snapshot["state_id"]
            or statuses[-1] != snapshot["status"]
        ):
            reasons.append("current_span_mismatch")
        if any(status not in {IssueStatus.BACKLOG, IssueStatus.TODO} for status in statuses):
            reasons.append("prior_ownership_or_hold_requires_reconciliation")
        if IssueStatus.BACKLOG in statuses[1:]:
            reasons.append("returned_backlog_requires_guarded_path")
        if snapshot["status"] not in {IssueStatus.BACKLOG, IssueStatus.TODO}:
            reasons.append("current_state_requires_guarded_path")
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        reasons.append("incomplete_or_invalid_history")
    if delivered:
        reasons.append("ever_delivered")
    if terminal:
        reasons.append("terminal_history")
    return Assessment(digest(snapshot), tuple(dict.fromkeys(reasons)), delivered, terminal, initial)
