"""One owner of dependency validity, graph checks, readiness and ordering."""

import math

from ai_task_manager.domains.work.errors import ValidationError
from ai_task_manager.domains.work.models import Scope

from .models import Blocker, Completion, Dependency, DependencySnapshot, Readiness


PRIORITIES = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "none": 4}
CONDITIONS = frozenset({"unit_verified", "code_merged", "work_done"})
MAX_RECEIPT_SECONDS = 30.0
MAX_EDGES = 256


def text(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and bool(value.strip())


def finite(value):
    return type(value) in {int, float} and math.isfinite(value)


def validate(snapshot, now):
    if type(snapshot) is not DependencySnapshot:
        raise ValidationError("A typed trusted dependency snapshot is required")
    if (
        type(snapshot.scope) is not Scope
        or any(not text(value) for value in vars(snapshot.scope).values())
        or not all(text(value) for value in (
            snapshot.task_id, snapshot.prose_digest, snapshot.source_id
        ))
        or (snapshot.canonical_issue_id is not None and not text(snapshot.canonical_issue_id))
        or type(snapshot.source_revision) is not int or snapshot.source_revision < 1
        or type(snapshot.previous_graph_version) is not int or snapshot.previous_graph_version < 0
        or snapshot.completeness not in {"complete", "unavailable"}
        or snapshot.priority not in PRIORITIES
        or type(snapshot.dependencies) is not tuple or len(snapshot.dependencies) > MAX_EDGES
        or not all(finite(value) for value in (snapshot.observed_at, snapshot.expires_at, now))
        or snapshot.observed_at > now
        or snapshot.expires_at <= snapshot.observed_at
        or snapshot.expires_at - snapshot.observed_at > MAX_RECEIPT_SECONDS
    ):
        raise ValidationError("Incomplete or unsupported dependency snapshot")
    keys = set()
    for edge in snapshot.dependencies:
        if (
            type(edge) is not Dependency or not text(edge.predecessor)
            or edge.predecessor == snapshot.task_id
            or edge.condition not in CONDITIONS
            or (edge.condition == "unit_verified" and not text(edge.unit))
            or (edge.condition != "unit_verified" and edge.unit is not None)
            or type(edge.evidence_refs) is not tuple
            or any(type(pair) is not tuple or len(pair) != 2
                   or not all(text(item) for item in pair) for pair in edge.evidence_refs)
            or len(dict(edge.evidence_refs)) != len(edge.evidence_refs)
        ):
            raise ValidationError("Invalid, self-referential or unsupported dependency")
        key = (edge.predecessor, edge.condition, edge.unit)
        if key in keys:
            raise ValidationError("Dependency declarations must be distinct")
        keys.add(key)
    return snapshot


def check_cycles(graph):
    # Iterative traversal does not borrow Python's recursion limit as a graph cap.
    visiting, visited = set(), set()
    for root in graph:
        stack = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                visiting.remove(node)
                visited.add(node)
            elif node not in visited:
                if node in visiting:
                    raise ValidationError("Dependency graph contains a cycle")
                visiting.add(node)
                stack.append((node, True))
                stack.extend((predecessor, False) for predecessor in graph.get(node, ()))


def evaluate(snapshot, completions, now):
    if snapshot is None:
        return Readiness("unknown", (Blocker(
            "dependency_metadata_unavailable", None,
            "Confirm a complete dependency declaration, including an explicit empty list",
        ),))
    try:
        validate(snapshot, now)
    except ValidationError:
        return Readiness("unknown", (Blocker(
            "dependency_snapshot_invalid", None,
            "Obtain a supported current dependency declaration",
        ),))
    if snapshot.completeness != "complete":
        return Readiness("unknown", (Blocker(
            "dependency_metadata_unavailable", None, "Confirm the complete dependency metadata"
        ),))
    if snapshot.expires_at <= now:
        return Readiness("unknown", (Blocker(
            "dependency_snapshot_expired", None, "Obtain a fresh trusted dependency snapshot"
        ),))
    blockers = []
    for edge in snapshot.dependencies:
        fact = completions.get((edge.predecessor, edge.condition, edge.unit))
        good = type(fact) is Completion and fact.availability == "verified"
        if good:
            good = fact.predecessor == edge.predecessor and fact.condition == edge.condition
        if good and edge.condition == "unit_verified":
            good = (fact.kind == "work_unit_complete" and fact.unit == edge.unit
                    and edge.unit in fact.completed_units)
        elif good:
            good = bool(fact.required_units) and set(fact.required_units) <= set(fact.completed_units)
            if edge.condition == "code_merged":
                good = good and fact.kind == "merge" and text(fact.commit)
            else:
                good = good and (fact.kind == "non_code_completion"
                                 or fact.kind == "production" and text(fact.artifact))
        if not good:
            allowed = {"failed", "canceled", "duplicate", "foreign", "missing", "stale",
                       "ambiguous", "unavailable", "held", "unknown"}
            reason = fact.availability if type(fact) is Completion and fact.availability in allowed else "unknown"
            blockers.append(Blocker(
                "dependency_" + reason,
                None if edge.predecessor.startswith("external:") else edge.predecessor,
                "Verify the declared " + edge.condition + " milestone with current authorized evidence",
            ))
    return Readiness("blocked" if blockers else "ready", tuple(blockers))


def ordered(items):
    """Items contain only authorized session task IDs, priority and queue sequence."""
    for task_id, priority, sequence in items:
        if not text(task_id) or priority not in PRIORITIES or type(sequence) is not int or sequence < 1:
            raise ValidationError("Priority and attributable original queue order are required")
    return sorted(items, key=lambda item: (PRIORITIES[item[1]], item[2], item[0]))
