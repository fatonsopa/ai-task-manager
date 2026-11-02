from dataclasses import replace

import pytest

from ai_task_manager.domains.checks.models import Fact
from ai_task_manager.domains.checks.reuse import eligible
from tests.checks.fixtures import change, plan, proof, sha, unit


def test_same_head_and_cross_head():
    original = plan()
    item = original.units[0]
    receipt = proof(original, item)
    assert eligible(original, item, receipt)
    later = replace(original, head="3" * 40, attempt="retry")
    assert eligible(later, item, receipt)
    assert not eligible(later, item, dict(receipt, physical_origin="missing"))
    assert not eligible(later, item, dict(receipt, passed=False))


def test_producer_change_invalidates_reuse():
    original = plan()
    item = original.units[0]
    receipt = proof(original, item)
    assert eligible(original, item, receipt)
    changed = replace(original, producer_digest=sha("changed dependency producer"))
    assert not eligible(changed, item, receipt)


def test_partial_scope():
    p = plan((unit("a"), unit("b")), changes=change(("src/a.txt", "src/b.txt")))
    a, b = p.units
    pa = proof(p, a)
    assert eligible(p, a, pa)
    assert not eligible(p, b, pa)
    changed = replace(b, inputs=(replace(b.inputs[0], value=sha("changed")), *b.inputs[1:]))
    assert not eligible(p, changed, proof(p, b))


@pytest.mark.parametrize("kind", ["source", "test", "config", "tool", "environment", "database"])
def test_relevant_input_changes(kind):
    original = plan()
    item = original.units[0]
    changed = replace(
        item,
        inputs=tuple(
            replace(f, value=sha("changed")) if f.kind == kind else f for f in item.inputs
        ),
    )
    assert not eligible(original, changed, proof(original, item))
    assert eligible(original, item, proof(original, item))


@pytest.mark.parametrize("kind", ["schema", "seed", "history"])
def test_database_equivalence(kind):
    facts = tuple(Fact("database", k, sha(k)) for k in ("schema", "seed", "history"))
    p = plan((unit(db=facts),))
    item = p.units[0]
    changed = replace(
        item,
        inputs=tuple(
            replace(f, value=sha("other")) if f.kind == "database" and f.name == kind else f
            for f in item.inputs
        ),
    )
    assert not eligible(p, changed, proof(p, item))
    assert eligible(p, item, proof(p, item))
    # Ephemeral physical process/resource identity is not a semantic database key.
    assert eligible(p, item, dict(proof(p, item), physical_resource="another-owned-generation"))


@pytest.mark.parametrize("value", [None, "absent", "rename"])
def test_absent_renamed_and_unavailable_inputs(value):
    p = plan()
    item = p.units[0]
    first = replace(
        item.inputs[0],
        value=value if value != "rename" else item.inputs[0].value,
        name="src/renamed.txt" if value == "rename" else item.inputs[0].name,
    )
    changed = replace(item, inputs=(first, *item.inputs[1:]))
    assert not eligible(p, changed, proof(p, item))


@pytest.mark.parametrize("field", ["principal", "project", "session", "task"])
def test_foreign_scope(field):
    p = plan()
    receipt = proof(p, p.units[0])
    receipt["scope"][field] = "foreign"
    assert not eligible(p, p.units[0], receipt)


def test_unknown_change_cannot_reuse():
    p = plan(changes=change(("unknown.txt",)))
    assert not eligible(p, p.units[0], proof(p, p.units[0]))
