"""Scheduling records only; application/domain code decides admission."""

import json
from dataclasses import asdict

from ai_task_manager.domains.scheduling.models import Dependency, DependencySnapshot
from ai_task_manager.domains.work.models import Scope

from .database import encode


def snapshot_from(data):
    data = dict(data)
    data["scope"] = Scope(**data["scope"])
    data["dependencies"] = tuple(
        Dependency(**{**edge, "evidence_refs": tuple(tuple(pair) for pair in edge["evidence_refs"])})
        for edge in data["dependencies"]
    )
    return DependencySnapshot(**data)


def current(conn, task_id):
    row = conn.execute("SELECT * FROM scheduling_snapshots WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "snapshot": snapshot_from(json.loads(row["data"]))}


def save(conn, snapshot, graph_version, digest):
    data = encode(asdict(snapshot))
    conn.execute(
        "INSERT INTO scheduling_history(task_id,graph_version,source_revision,digest,data) VALUES(?,?,?,?,?)",
        (snapshot.task_id, graph_version, snapshot.source_revision, digest, data),
    )
    conn.execute(
        "INSERT INTO scheduling_snapshots VALUES(?,?,?,?,?,NULL) "
        "ON CONFLICT(task_id) DO UPDATE SET graph_version=excluded.graph_version,"
        "source_revision=excluded.source_revision,digest=excluded.digest,data=excluded.data,invalidated_by=NULL",
        (snapshot.task_id, graph_version, snapshot.source_revision, digest, data),
    )
    conn.execute("DELETE FROM scheduling_edges WHERE task_id=?", (snapshot.task_id,))
    conn.executemany("INSERT INTO scheduling_edges VALUES(?,?,?,?)", [
        (snapshot.task_id, edge.predecessor, edge.condition, edge.unit or "")
        for edge in snapshot.dependencies
    ])


def graph(conn, session_id):
    return {
        row[0]: tuple(x[0] for x in conn.execute(
            "SELECT predecessor FROM scheduling_edges WHERE task_id=?", (row[0],)
        ))
        for row in conn.execute("SELECT id FROM tasks WHERE session_id=?", (session_id,)).fetchall()
    }


def bind_attempt(conn, attempt_id, task_id, graph_version):
    conn.execute("INSERT INTO scheduling_attempts VALUES(?,?,?,NULL)", (attempt_id, task_id, graph_version))


def attempt_binding(conn, attempt_id):
    row = conn.execute("SELECT * FROM scheduling_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    return dict(row) if row else None


def invalidate(conn, task_id, event_id, reason):
    conn.execute("UPDATE scheduling_snapshots SET invalidated_by=? WHERE task_id=?", (event_id, task_id))
    conn.execute(
        "UPDATE scheduling_attempts SET invalidated_by=? WHERE task_id=? AND invalidated_by IS NULL",
        (event_id, task_id),
    )
    return conn.execute("INSERT OR IGNORE INTO scheduling_wakes VALUES(?,?,?)", (task_id, event_id, reason)).rowcount == 1


def dependents(conn, predecessor, session_id):
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT scheduling_edges.task_id FROM scheduling_edges "
        "JOIN tasks ON tasks.id=scheduling_edges.task_id "
        "WHERE predecessor=? AND tasks.session_id=?", (predecessor, session_id)
    ).fetchall()]


def queue_sequence(conn, task_id):
    intents = set()
    for row in conn.execute("SELECT sequence,kind,payload FROM events WHERE task_id=? ORDER BY sequence", (task_id,)):
        payload = json.loads(row[2])
        version, revision = payload.get("task_version"), payload.get("decision_revision")
        if (row[1] == "transition_requested" and payload.get("cause") in {"enqueue", "review_decision"}
                and type(version) is int and type(revision) is int):
            intents.add((version, revision))
        if (row[1] == "existing_todo_queue_confirmed"
                and payload.get("source_ref") and payload.get("decision_ref")):
            return row[0]
        if (row[1] == "provider_status_confirmed" and payload.get("observed_status") == "Todo"
                and type(version) is int and type(revision) is int
                and (version - 1, revision) in intents):
            return row[0]
    return None
