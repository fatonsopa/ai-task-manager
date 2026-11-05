"""Finite target-advance lineage; original worker and decision authority never renew."""

from dataclasses import asdict, replace

from ai_task_manager.config.files import digest
from ai_task_manager.domains.merging.candidates import CandidateGrant
from ai_task_manager.domains.merging.contracts import Binding
from ai_task_manager.domains.work.errors import AccessDenied

from .models import MergePlan


def restore(value):
    grant = dict(value["grant"])
    grant["binding"] = Binding(**grant["binding"])
    return MergePlan(**dict(value, grant=CandidateGrant(**grant)))


def successor(plan, target):
    grant = plan.grant
    key = "candidate-target:" + digest([asdict(grant.binding), target])
    return replace(
        plan,
        grant=replace(grant, target_head=target, binding=replace(grant.binding, operation_key=key)),
    )


def current_plan(original, row, *, maximum_attempts=1):
    if row["request"].get("maximum_attempts", 1) != maximum_attempts:
        raise AccessDenied("Saved merge attempt budget differs from the installed source")
    selected = original
    history = row.get("target_lineage", [])
    if (
        type(maximum_attempts) is not int
        or not 1 <= maximum_attempts <= 4
        or len(history) >= maximum_attempts
    ):
        raise AccessDenied("Original merge target-advance budget exhausted")
    for previous in history:
        if any(
            name in previous["steps"]
            for name in ("publication_intent", "publication_fence", "publication", "delivery")
        ):
            raise AccessDenied("Frozen publication history cannot become target lineage")
        if previous["plan"] != asdict(selected):
            raise AccessDenied("Saved target lineage differs from original merge authority")
        selected = successor(selected, previous["target"])
    if row.get("active_plan", asdict(original)) != asdict(selected):
        raise AccessDenied("Saved current merge plan differs from its exact target lineage")
    return selected


def recorded_plan(row, *, maximum_attempts=1):
    """Resolve a successor only through its immutable original request binding."""
    history = row.get("target_lineage", [])
    if not history:
        raise AccessDenied("A successor requires retained original target lineage")
    original = restore(history[0]["plan"])
    if (row["request"]["plan"], row["request"]["grant"]) != (
        original.digest,
        asdict(original.grant),
    ):
        raise AccessDenied("Target lineage differs from canonical original request")
    return current_plan(original, row, maximum_attempts=maximum_attempts)
