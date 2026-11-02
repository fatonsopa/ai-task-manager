from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from threading import Barrier

import pytest

from ai_task_manager.adapters.projects.ai_index.contracts import Binding
from ai_task_manager.adapters.projects.ai_index.journal import Journal
from ai_task_manager.adapters.projects.ai_index.registrations import Registrations
from ai_task_manager.domains.work.errors import IdempotencyConflict, StaleVersion, WorkError

FIELDS = [f.name for f in fields(Binding)]
VARIANTS = ["changed_" + f for f in FIELDS] + ["missing_" + f for f in FIELDS]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_exact_registered_binding(harness, variant):
    mode, field = variant.split("_", 1)
    value = "" if mode == "missing" else ("f" * 64 if field.endswith("_digest") else "foreign")
    changed = replace(harness.binding, **{field: value})
    before = harness.journal.events(harness.ref)
    with pytest.raises(WorkError):
        harness.registrations.register(changed)
    with pytest.raises(WorkError):
        harness.registrations.resolve(harness.ref, changed)
    assert harness.journal.events(harness.ref) == before


VARIANTS = ["identical_replay", "changed_input", "concurrent_registration", "historical_receipt"]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_replay_and_racing_registration(harness, variant):
    original = harness.registration
    if variant == "changed_input":
        changed = replace(harness.binding, command_id="changed")
        harness.authority.allowed = changed
        with pytest.raises(IdempotencyConflict):
            harness.registrations.register(changed)
        harness.journal.commit(harness.ref, 0, "first", "recorded", {"x": 1})
        with pytest.raises(StaleVersion):
            harness.journal.commit(harness.ref, 0, "other", "recorded", {"x": 2})
        with pytest.raises(IdempotencyConflict):
            harness.journal.commit(harness.ref, 0, "first", "recorded", {"x": 2})
    elif variant == "concurrent_registration":
        barrier = Barrier(2)
        racing = replace(harness.binding, operation_key="fresh-racing-operation")
        harness.authority.allowed = racing

        def register(_):
            journal = Journal(harness.journal.path)
            registry = Registrations(journal, harness.authority)
            barrier.wait(timeout=3)
            return registry.register(racing)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, range(2)))
        assert results[0] == results[1]
        assert results[0]["ref"] != original["ref"]
        assert (
            len([e for e in harness.journal.events(results[0]["ref"]) if e["kind"] == "registered"])
            == 1
        )
    else:
        if variant == "historical_receipt":
            harness.operations.intend_launch(harness.ref, 0, "launch")
            assert harness.version() == 1
        assert harness.registrations.register(harness.binding) == original
        assert original["version"] == 0
    assert len([e for e in harness.journal.events(harness.ref) if e["kind"] == "registered"]) == 1
