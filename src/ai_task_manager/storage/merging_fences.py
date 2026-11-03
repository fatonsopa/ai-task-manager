"""Canonical pending publication facts in the existing append-only Core event stream."""

import json
from dataclasses import asdict

from ai_task_manager.domains.work.errors import EvidenceRequired


def pending(conn, issue_key):
    states = {}
    for (raw,) in conn.execute("SELECT payload FROM events WHERE kind='publication_fence_updated'"):
        value = json.loads(raw)
        if value["issue_key"] == issue_key:
            reference = value["context"]["reference"]
            if reference not in states or value["state"] == "committed":
                states[reference] = value
    return [row for row in states.values() if row["state"] == "pending"]


def require_clear(conn, issue_key, context=None):
    rows = pending(conn, issue_key)
    if rows and (context is None or len(rows) != 1 or rows[0]["context"] != asdict(context)):
        raise EvidenceRequired("Pending or uncertain publication prevents new work")
    if context is not None and not rows:
        raise EvidenceRequired("The exact pending publication authority is unavailable")
