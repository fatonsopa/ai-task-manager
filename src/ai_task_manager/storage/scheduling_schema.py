"""Additive schema and ordinary old-writer compatibility fence, not a sandbox."""

import json

from ai_task_manager.domains.work.errors import SchemaError
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS, AttemptState


PROTOCOL = 4
FUNCTION = "aitm_scheduling_writer_protocol"
TABLES = {
    "scheduling_snapshots": "task_id TEXT PRIMARY KEY REFERENCES tasks(id), graph_version INTEGER NOT NULL, source_revision INTEGER NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL, invalidated_by TEXT",
    "scheduling_history": "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id), graph_version INTEGER NOT NULL, source_revision INTEGER NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL",
    "scheduling_edges": "task_id TEXT NOT NULL REFERENCES tasks(id), predecessor TEXT NOT NULL, condition TEXT NOT NULL, unit TEXT NOT NULL, PRIMARY KEY(task_id,predecessor,condition,unit)",
    "scheduling_attempts": "attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), task_id TEXT NOT NULL REFERENCES tasks(id), graph_version INTEGER NOT NULL, invalidated_by TEXT",
    "scheduling_wakes": "task_id TEXT NOT NULL REFERENCES tasks(id), source_event TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(task_id,source_event)",
}


def register(conn):
    # Pure constant with no I/O or callback authority. Old connections lack it.
    conn.create_function(FUNCTION, 0, lambda: PROTOCOL, deterministic=True)


def trigger_sql(table, operation):
    name = "aitm_schedule_" + table + "_" + operation.lower()
    return (f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
            f"BEGIN SELECT CASE WHEN {FUNCTION}() != {PROTOCOL} "
            "THEN RAISE(ABORT,'Unsupported scheduling writer protocol') END; END")


def operational_tables(conn):
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('metadata','store_identity') ORDER BY name"
    )]


def require_quiescent(conn):
    for row in conn.execute("SELECT id,task_id,session_id,host,active,data FROM attempts"):
        try:
            data = json.loads(row[5])
            state = AttemptState(data["state"])
            if any(data[key] != row[index] for index, key in enumerate(
                ("id", "task_id", "session_id", "host")
            )):
                raise ValueError("Attempt index and record differ")
        except (ValueError, KeyError, TypeError) as exc:
            raise SchemaError("Attempt ownership is unavailable for migration") from exc
        if row[4] or state in ACTIVE_ATTEMPTS:
            raise SchemaError("Active or unresolved attempts prevent scheduling migration")
    if conn.execute("SELECT 1 FROM resource_leases LIMIT 1").fetchone():
        raise SchemaError("Unsettled owned resources prevent scheduling migration")


def migrate(conn):
    version = conn.execute("SELECT version FROM metadata").fetchall()
    if len(version) != 1 or version[0][0] not in {3, 4}:
        raise SchemaError("Scheduling migration requires schema 3 or 4")
    if version[0][0] == 4:
        validate(conn)
        return
    require_quiescent(conn)
    # Existing task JSON, store identity and all historical records remain unchanged.
    for table, columns in TABLES.items():
        conn.execute(f"CREATE TABLE {table}({columns})")
    for table in operational_tables(conn):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(trigger_sql(table, operation))
    conn.execute("UPDATE metadata SET version=4")
    validate(conn)


def validate(conn):
    for table, columns in TABLES.items():
        definition = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if definition is None or definition[0] != f"CREATE TABLE {table}({columns})":
            raise SchemaError("Scheduling table definition is missing or changed")
        actual = conn.execute(f"PRAGMA table_info({table})").fetchall()
        expected = [part.strip().split()[0] for part in columns.split(", ")
                    if not part.startswith("PRIMARY KEY")]
        if [row[1] for row in actual] != expected:
            raise SchemaError("Incomplete scheduling schema")
    for table in operational_tables(conn):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            expected = trigger_sql(table, operation)
            name = "aitm_schedule_" + table + "_" + operation.lower()
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            if row is None or row[0].rstrip(";") != expected.rstrip(";"):
                raise SchemaError("Scheduling writer protocol fence is missing or changed")
