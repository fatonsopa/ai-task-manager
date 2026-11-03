from dataclasses import replace

import pytest

from ai_task_manager.domains.checks.models import Availability, Fact
from ai_task_manager.domains.checks.selection import select
from ai_task_manager.domains.work.errors import EvidenceRequired, ValidationError
from tests.checks.fixtures import (
    SCOPE,
    SOURCE,
    catalog,
    change,
    discovery,
    plan,
    unit,
)


@pytest.mark.parametrize("status", [Availability.UNAVAILABLE, Availability.UNSUPPORTED])
def test_discovery_availability(status):
    with pytest.raises(EvidenceRequired):
        select(catalog(), SCOPE, "attempt", SOURCE, change(), discovery(status=status))
    with pytest.raises(EvidenceRequired):
        select(catalog(), SCOPE, "attempt", SOURCE, change(status=status), discovery())
    empty = select(catalog(), SCOPE, "attempt", SOURCE, change(()), discovery(()))
    assert empty.units == () and empty.groups == ()


def test_catalog_group_selection():
    result = plan(
        (unit("a", "ALIAS A"), unit("b", "checks b"), unit("c", "checks c")),
        changes=change(("src/a.txt", "src/c.txt")),
    )
    assert [(u.name, u.group) for u in result.units] == [("a", "checks a"), ("c", "checks c")]
    assert [(g.key, g.category) for g in result.groups] == [
        ("checks a", "text-consistency"),
        ("checks b", "text-consistency"),
        ("checks c", "syntax"),
    ]
    assert result.reasons == (("b", "No changed declared input"),)


def test_unknown_change_forces_execution():
    result = plan((unit("a"), unit("b")), changes=change(("unknown/path.txt",)))
    assert {u.name for u in result.units} == {"a", "b"}
    assert result.cacheable is False


@pytest.mark.parametrize("known", [False, True], ids=["unknown_path", "known_path"])
def test_nonempty_change_requires_inventory(known):
    observed = discovery(())
    observed = replace(observed, known_paths=("src/a.txt",) if known else ())
    with pytest.raises(EvidenceRequired):
        select(catalog(), SCOPE, "attempt", SOURCE, change(), observed)


def test_known_empty_selection_keeps_complete_inventory():
    result = plan(changes=change(("unrelated.txt",)))
    assert result.units == () and result.inventory_size == 1
    assert result.selection_availability == "known_empty"
    assert result.change_paths == ("unrelated.txt",)
    assert result.reasons == (("a", "No changed declared input"),)


def test_prerequisites_select_and_order_required_units():
    result = plan(
        (unit("a", "checks a"), unit("b", "checks b")),
        changes=change(("src/b.txt",)),
        prerequisites=(("checks b", ("checks a",)),),
    )
    assert {u.name for u in result.units} == {"a", "b"}
    assert [g.key for g in result.groups] == ["checks a", "checks b"]
    with pytest.raises(ValidationError):
        plan(
            (unit("a"), unit("b", "checks b")),
            prerequisites=(("checks a", ("checks b",)), ("checks b", ("checks a",))),
        )


@pytest.mark.parametrize("mutation", ["duplicate", "traversal", "unknown_input", "mutable"])
def test_invalid_discovery(mutation):
    d, c = discovery(), change()
    if mutation == "duplicate":
        d = replace(d, units=(unit(), unit()))
    elif mutation == "traversal":
        c = change(("../foreign",))
    elif mutation == "unknown_input":
        u = unit()
        d = discovery((replace(u, inputs=(replace(u.inputs[0], value=None), *u.inputs[1:])),))
    else:
        d = replace(d, units=list(d.units))
    with pytest.raises((EvidenceRequired, ValidationError)):
        select(catalog(), SCOPE, "attempt", SOURCE, c, d)


@pytest.mark.parametrize("path", ["../foreign", "/foreign", "src/../foreign"])
def test_input_paths_are_repo_relative(path):
    with pytest.raises(ValidationError):
        Fact("source", path, "a" * 64).validate()
