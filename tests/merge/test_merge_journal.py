import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from unit_fixtures import Harness

from ai_task_manager.application.merging.service import MergeService
from ai_task_manager.domains.work.errors import IdempotencyConflict, SchemaError, StaleVersion
from ai_task_manager.storage.merging.journal import Journal


@pytest.fixture
def h(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return Harness(private / "merge.sqlite")


@pytest.mark.parametrize("stage", ["after_state", "after_event", "before_commit", "after_commit"])
def test_intent_event_and_replay_are_atomic(h, stage):
    before = h.snapshot()

    class Fault:
        def hit(self, current):
            if current == stage:
                raise RuntimeError("authored interruption")

    h.journal.faults = Fault()
    with pytest.raises(RuntimeError):
        h.journal.commit(h.ref, 0, "effect", "intent", {"marker": "value"})
    reopened = Journal(h.journal.path)
    if stage == "after_commit":
        replay = reopened.commit(h.ref, 0, "effect", "intent", {"marker": "value"})
        assert replay["version"] == 1
        assert len(reopened.events(h.ref)) == len(before[1]) + 1
        assert reopened.current(h.ref)["facts"] == {"marker": "value"}
    else:
        assert (reopened.current(h.ref), reopened.events(h.ref)) == before


def test_registration_race_has_one_operation_and_event(h):
    barrier = Barrier(2)

    def register(_):
        service = MergeService(Journal(h.journal.path), h.authority)
        barrier.wait(timeout=5)
        return service.register(h.binding)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(register, range(2)))
    assert receipts == [h.registration, h.registration]
    assert len(h.journal.events(h.ref)) == 1


def test_version_and_idempotency_conflicts_preserve_history(h):
    changed = replace(h.binding, source_digest="f" * 64)
    h.authority.allowed = changed
    with pytest.raises(IdempotencyConflict):
        h.service.register(changed)
    first = h.journal.commit(h.ref, 0, "first", "intent", {"x": 1})
    assert h.journal.commit(h.ref, 0, "first", "intent", {"x": 1}) == first
    with pytest.raises(IdempotencyConflict):
        h.journal.commit(h.ref, 0, "first", "intent", {"x": 2})
    with pytest.raises(StaleVersion):
        h.journal.commit(h.ref, 0, "other", "intent", {"x": 3})
    assert h.journal.current(h.ref)["facts"] == {"x": 1}


@pytest.mark.parametrize("table", ["foreign_store", "ai_meta"])
def test_foreign_and_retired_store_are_not_silently_converted(tmp_path, table):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    p = root / "historical.sqlite"
    conn = sqlite3.connect(p)
    conn.execute(f"CREATE TABLE {table}(preserved TEXT)")
    conn.execute(f"INSERT INTO {table} VALUES('history')")
    conn.commit()
    conn.close()
    p.chmod(0o600)
    before = p.read_bytes()
    with pytest.raises(SchemaError):
        Journal(p)
    assert p.read_bytes() == before
