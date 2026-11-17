"""Successor protocol one fences pre-successor writers while retaining history reads."""

from ai_task_manager.domains.work.errors import SchemaError

from .scheduling_schema import operational_tables

PROTOCOL = 1
FUNCTION = "aitm_successor_writer_protocol"


def register(conn):
    conn.create_function(FUNCTION, 0, lambda: PROTOCOL, deterministic=True)


def trigger_sql(table, operation):
    return (
        f"CREATE TRIGGER aitm_successor_{table}_{operation.lower()} "
        f"BEFORE {operation} ON {table} BEGIN SELECT CASE WHEN {FUNCTION}() != {PROTOCOL} "
        "THEN RAISE(ABORT,'Unsupported successor writer protocol') END; END"
    )


def install(conn):
    for table in operational_tables(conn):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(trigger_sql(table, operation))


def validate(conn):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_capabilities'"
    ).fetchone():
        return
    for table in operational_tables(conn):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"aitm_successor_{table}_{operation.lower()}"
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()
            if row is None or row[0].rstrip(";") != trigger_sql(table, operation).rstrip(";"):
                raise SchemaError("Successor writer fence is missing or changed")
