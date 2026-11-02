"""Reuse physical-origin evidence only within its exact proven unit scope."""

from dataclasses import asdict


def eligible(plan, unit, proof):
    """Proofs come from the enrolled result store, not a caller-supplied green flag."""
    if not plan.cacheable or not unit.reusable:
        return False
    return (
        proof.get("scope") == asdict(plan.scope)
        and proof.get("unit") == unit.name
        and proof.get("group") == unit.group
        and proof.get("input_digest") == unit.input_digest
        and proof.get("catalog_digest") == plan.catalog_digest
        and proof.get("producer_digest") == plan.producer_digest
        and proof.get("physical_origin") == proof.get("result_ref")
        and type(proof.get("result_ref")) is str
        and proof.get("source_digest") == plan.source_digest
        and proof.get("passed") is True
    )
