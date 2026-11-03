"""Authored Core4 history fixtures and actual Core5 migration/lookup controls."""

import json
import sqlite3
from copy import deepcopy
from uuid import uuid4

import pytest

from ai_task_manager.domains.work.errors import EvidenceRequired, SchemaError
from ai_task_manager.domains.work.models import Attempt, AttemptState
from ai_task_manager.storage import (
    attempts,
    events,
    issue_claims,
    merging_fences,
    publication_schema,
)
from ai_task_manager.storage.database import Database
from ai_task_manager.storage.scheduling_schema import register as register_v4
from tests.domains.helpers import Rig


def fact(rig, task, *, state="pending"):
    intent = {
        "task_id": task.id,
        "attempt_id": "authored-done-attempt",
        "issue_id": task.issue_id,
        "prose_digest": task.prose_digest,
        "task_version": task.version,
        "attempt_version": 1,
        "decision_revision": task.decision_revision,
        "decision_ref": "authored:decision",
        "operation_ref": "authored:publication",
        "binding_digest": "a" * 64,
        "commit": "b" * 40,
        "old": "c" * 40,
        "source_ref": "refs/heads/worker",
        "source_head": "d" * 40,
        "target_ref": "refs/heads/integration",
        "candidate_ref": "refs/heads/candidate",
        "checks_ref": "authored:checks",
    }
    value = {
        "issue_key": issue_claims.identity(rig.scope, task.issue_id),
        "context": {
            "reference": "publication-fence:" + str(uuid4()),
            "binding_digest": intent["binding_digest"],
            "commit": intent["commit"],
        },
        "intent": intent,
        "state": state,
    }
    if state == "committed":
        value["actual_merge_ref"] = "merge-publication:authored-history"
    return value


def v4_store(path, *, state="pending", bad=None):
    rig = Rig(path)
    task = rig.task()
    # The additive5 projection is removed to construct the exact prior4 schema;
    # every old table, protocol4 trigger, row and store identity is retained.
    with rig.db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        names = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
            if r[0].startswith("aitm_publication_")
        ]
        for name in names:
            conn.execute('DROP TRIGGER "' + name + '"')
        conn.execute("DROP TABLE publication_fences")
        conn.execute("UPDATE metadata SET version=4")
        value = fact(rig, task)
        events.append(conn, task.session_id, task.id, "publication_fence_updated", value)
        if state == "committed":
            events.append(
                conn,
                task.session_id,
                task.id,
                "publication_fence_updated",
                dict(
                    value, state="committed", actual_merge_ref="merge-publication:authored-history"
                ),
            )
        if bad:
            altered = deepcopy(value)
            if bad == "state":
                altered["state"] = "unknown"
            elif bad == "conflict":
                altered["context"]["reference"] = "publication-fence:" + str(uuid4())
            elif bad == "canonical":
                altered["issue_key"] = "f" * 64
            events.append(conn, task.session_id, task.id, "publication_fence_updated", altered)
        conn.commit()
    return rig, task, value


def original_rows(path):
    with sqlite3.connect(path) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if r[0] not in {"metadata", "publication_fences", "issue_lifecycle"}
        ]
        return {
            name: conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
            for name in tables
        }


@pytest.mark.parametrize("state", ["pending", "committed"])
def test_actual_v4_history_backfill_preserves_original_records_and_canonical_fact(tmp_path, state):
    rig, task, value = v4_store(tmp_path / "core.sqlite", state=state)
    before = original_rows(rig.db.path)
    db = Database(rig.db.path)
    assert original_rows(db.path) == before
    with db.transaction() as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 5
        current = merging_fences.current(conn, value["issue_key"])
        assert current["context"] == value["context"] and current["intent"] == value["intent"]
        assert current["state"] == state
        assert conn.execute(
            "SELECT count(*) FROM events WHERE kind='publication_fence_updated'"
        ).fetchone()[0] == (2 if state == "committed" else 1)
        if state == "committed":
            assert issue_claims.known_delivered(conn, rig.scope, task)
        else:
            with pytest.raises(EvidenceRequired):
                issue_claims.require_open(conn, rig.scope, task.issue_id)


@pytest.mark.parametrize("bad", ["state", "conflict", "canonical"])
def test_malformed_or_ambiguous_history_rolls_back_whole_migration(tmp_path, bad):
    rig, _, _ = v4_store(tmp_path / "core.sqlite", bad=bad)
    before = original_rows(rig.db.path)
    with pytest.raises(SchemaError):
        Database(rig.db.path)
    assert original_rows(rig.db.path) == before
    with sqlite3.connect(rig.db.path) as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 4
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='publication_fences'"
        ).fetchone()


def test_actual_preopened_v4_connection_cannot_write_after_upgrade(tmp_path):
    rig, task, _ = v4_store(tmp_path / "core.sqlite")
    old = sqlite3.connect(rig.db.path, isolation_level=None)
    register_v4(old)
    try:
        assert old.execute("SELECT version FROM metadata").fetchone()[0] == 4
        before = original_rows(rig.db.path)
        Database(rig.db.path)
        with pytest.raises(sqlite3.DatabaseError, match="aitm_publication_writer_protocol"):
            old.execute(
                "INSERT INTO operations VALUES(?,?,?,?)",
                ("stale-v4-write", task.session_id, "d", "{}"),
            )
        assert original_rows(rig.db.path) == before
    finally:
        old.close()


def test_late_migration_failure_rolls_back_projection_events_and_identity(tmp_path, monkeypatch):
    rig, _, _ = v4_store(tmp_path / "core.sqlite")
    before = original_rows(rig.db.path)
    seen = []

    def interrupt(conn):
        seen.append(conn.execute("SELECT version FROM metadata").fetchone()[0])
        raise RuntimeError("Authored after-backfill validation interruption")

    monkeypatch.setattr(publication_schema, "validate", interrupt)
    with pytest.raises(RuntimeError):
        Database(rig.db.path)
    assert seen == [5] and original_rows(rig.db.path) == before
    with sqlite3.connect(rig.db.path) as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 4
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='publication_fences'"
        ).fetchone()


def test_active_prior_worker_prevents_upgrade_without_losing_ownership(tmp_path):
    rig, task, _ = v4_store(tmp_path / "core.sqlite")
    conn = sqlite3.connect(rig.db.path, isolation_level=None)
    register_v4(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        attempts.insert(
            conn,
            Attempt(
                "active-old-worker", task.id, task.session_id, "fixture", AttemptState.RUNNING, 0
            ),
        )
        conn.commit()
    finally:
        conn.close()
    before = original_rows(rig.db.path)
    with pytest.raises(SchemaError):
        Database(rig.db.path)
    assert original_rows(rig.db.path) == before


def test_real_lookup_uses_canonical_index_without_scanning_foreign_projection_rows(tmp_path):
    rig = Rig(tmp_path / "core.sqlite")
    with rig.db.transaction() as conn:
        for number in range(1000):
            conn.execute(
                "INSERT INTO publication_fences VALUES(?,?,?,?)",
                (
                    str(number).zfill(64),
                    "unrelated:" + str(number),
                    "pending",
                    "malformed foreign bytes must stay out of this lookup",
                ),
            )
        statements = []
        conn.set_trace_callback(statements.append)
        assert merging_fences.pending(conn, "f" * 64) == []
        conn.set_trace_callback(None)
        queries = [s for s in statements if s.startswith("SELECT")]
        assert len(queries) == 1 and "WHERE issue_key=" in queries[0]
        assert "events" not in queries[0]
        plan = conn.execute("EXPLAIN QUERY PLAN " + queries[0]).fetchall()
        assert len(plan) == 1 and "SEARCH" in plan[0][3] and "USING INDEX" in plan[0][3]


@pytest.mark.parametrize("corruption", ["state", "json", "index"])
def test_corrupt_relevant_projection_fails_closed(tmp_path, corruption):
    from ai_task_manager.domains.work.errors import WorkError

    rig = Rig(tmp_path / "core.sqlite")
    task = rig.task()
    value = fact(rig, task)
    with rig.db.transaction() as conn:
        merging_fences.insert(conn, value)
        if corruption == "state":
            altered = dict(value, state="unknown")
            conn.execute("UPDATE publication_fences SET data=?", (json.dumps(altered),))
        elif corruption == "json":
            conn.execute("UPDATE publication_fences SET data='{' ")
        else:
            conn.execute("UPDATE publication_fences SET context_ref='different-context'")
        with pytest.raises(WorkError):
            issue_claims.require_open(conn, rig.scope, task.issue_id)
