"""Canonical publication state and the current Core admission writer fence."""

from ai_task_manager.domains.work.errors import SchemaError
from ai_task_manager.domains.work.models import Scope
from ai_task_manager.storage import issue_claims
from ai_task_manager.storage.scheduling_schema import (
    operational_tables,
    require_quiescent,
    trigger_sql,
)

PROTOCOL = 6
FUNCTION = "aitm_publication_writer_protocol"
COLUMNS = (
    "issue_key TEXT PRIMARY KEY NOT NULL,context_ref TEXT NOT NULL UNIQUE,"
    "state TEXT NOT NULL CHECK(state IN ('pending','committed')),data TEXT NOT NULL"
)


def register(conn):
    conn.create_function(FUNCTION, 0, lambda: PROTOCOL, deterministic=True)


def writer_trigger(table, operation, *, protocol=PROTOCOL):
    return (
        f"CREATE TRIGGER aitm_publication_{table}_{operation.lower()} "
        f"BEFORE {operation} ON {table} "
        f"BEGIN SELECT CASE WHEN {FUNCTION}() != {protocol} "
        "THEN RAISE(ABORT,'Unsupported publication writer protocol') END; END"
    )


def validate(conn, *, protocol=PROTOCOL):
    expected = "CREATE TABLE publication_fences(" + COLUMNS + ")"
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='publication_fences'"
    ).fetchone()
    if row is None or row[0] != expected:
        raise SchemaError("Canonical publication projection is missing or changed")
    for table in operational_tables(conn):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"aitm_publication_{table}_{operation.lower()}"
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()
            if (row is None or row[0].rstrip(";") !=
                    writer_trigger(table, operation, protocol=protocol).rstrip(";")):
                raise SchemaError("Publication writer protocol fence is missing or changed")


def migrate(conn):
    version = conn.execute("SELECT version FROM metadata").fetchone()[0]
    if version == 6:
        validate(conn)
        return
    if version == 5:
        validate(conn, protocol=5)
        for table in operational_tables(conn):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"DROP TRIGGER aitm_publication_{table}_{operation.lower()}")
                conn.execute(writer_trigger(table, operation))
        conn.execute("UPDATE metadata SET version=6")
        validate(conn)
        return
    if version != 4:
        raise SchemaError("Publication migration requires Core schema4")
    require_quiescent(conn)
    conn.execute("CREATE TABLE publication_fences(" + COLUMNS + ")")
    from ai_task_manager.config.files import strict_json
    from ai_task_manager.storage.merging_fences import backfill, decode

    # This is the one-time history interpretation, never a work/lease read path.
    for row in conn.execute(
        "SELECT e.task_id,e.payload,s.scope FROM events e "
        "JOIN sessions s ON s.id=e.session_id WHERE e.kind='publication_fence_updated' "
        "ORDER BY e.task_id,e.sequence"
    ):
        fact = decode(row[1])
        try:
            scope = Scope(**strict_json(row[2]))
        except (ValueError, TypeError) as exc:
            raise SchemaError("Publication history scope is malformed") from exc
        if (
            fact["intent"]["task_id"] != row[0]
            or issue_claims.identity(scope, fact["intent"]["issue_id"]) != fact["issue_key"]
        ):
            raise SchemaError("Publication history canonical scope is inconsistent")
        backfill(conn, fact)
        if fact["state"] == "committed":
            issue_claims.fence(
                conn,
                scope,
                fact["intent"]["issue_id"],
                delivered=True,
                source=fact["actual_merge_ref"],
            )
    for operation in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(trigger_sql("publication_fences", operation))
    for table in operational_tables(conn):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(writer_trigger(table, operation))
    conn.execute("UPDATE metadata SET version=6")
    validate(conn)
