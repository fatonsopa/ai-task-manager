"""Preserve schema-five owners while fencing old fresh and preopened writers."""

import sqlite3
import subprocess
import types

import pytest

from ai_task_manager.domains.work.errors import SchemaError
from ai_task_manager.storage import publication_schema
from ai_task_manager.storage.database import Database
from ai_task_manager.storage.scheduling_schema import operational_tables

from .test_guards import reserve


def snapshot(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master "
              "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='metadata'")]
    return {name: [tuple(row) for row in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
            for name in tables}


def schema_five(rig):
    with rig.db.transaction() as conn:
        for table in operational_tables(conn):
            for op in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"DROP TRIGGER aitm_publication_{table}_{op.lower()}")
                conn.execute(publication_schema.writer_trigger(table, op, protocol=5))
        conn.execute("UPDATE metadata SET version=5")


@pytest.mark.parametrize("interrupted", [False, True])
def test_preserves_active_unknown_rows_and_atomically_fences_schema_five_writers(rig, monkeypatch, interrupted):
    task = rig.queued()
    attempt = reserve(rig, task)
    with rig.db.transaction() as conn:
        conn.execute("DELETE FROM resource_leases WHERE kind='admission'")
    schema_five(rig)
    old = sqlite3.connect(rig.db.path)
    old.create_function("aitm_scheduling_writer_protocol", 0, lambda: 4)
    old.create_function("aitm_publication_writer_protocol", 0, lambda: 5)
    old.create_function("aitm_successor_writer_protocol", 0, lambda: 1)
    old.execute("UPDATE sessions SET revoked=revoked")
    old.commit()
    before = snapshot(old)
    original = publication_schema.writer_trigger
    if interrupted:
        def fail(table, operation, *, protocol=6):
            if protocol == 6:
                raise RuntimeError("authored fence replacement interruption")
            return original(table, operation, protocol=protocol)
        monkeypatch.setattr(publication_schema, "writer_trigger", fail)
        with pytest.raises(RuntimeError, match="fence replacement interruption"):
            Database(rig.db.path)
        assert old.execute("SELECT version FROM metadata").fetchone()[0] == 5
        old.execute("UPDATE sessions SET revoked=revoked")
        old.commit()
        assert snapshot(old) == before
        monkeypatch.undo()
    upgraded = Database(rig.db.path)
    assert old.execute("SELECT version FROM metadata").fetchone()[0] == 6
    assert snapshot(old) == before
    with pytest.raises(sqlite3.DatabaseError, match="publication writer protocol"):
        old.execute("UPDATE sessions SET revoked=revoked")
    old.rollback()
    # Exercise the exact accepted pre-DP06 Database opener, not a new imitation.
    source = subprocess.check_output([
        "git", "show", "be648a1fd5199a598152f84641fc489dec067d4e:src/ai_task_manager/storage/database.py"
    ], text=True)
    module = types.ModuleType("accepted_schema_five_database")
    exec(compile(source, "accepted-schema-five-database.py", "exec"), module.__dict__)
    with pytest.raises(SchemaError, match="Unsupported task store schema"):
        module.Database(rig.db.path)
    with upgraded.transaction() as conn:
        assert conn.execute("SELECT active FROM attempts WHERE id=?", (attempt.id,)).fetchone()[0] == 1
        assert snapshot(conn) == before
    old.close()
