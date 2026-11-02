"""One scoped application read supplies sidebar and passive status output."""

from collections import Counter
from dataclasses import dataclass

from .text import literal

ACTIVITIES = {
    "queued": ("◻", "[ ]", "Queued", "default"),
    "reserved": ("◌", "( )", "Reserved", "default"),
    "starting": ("↗", "^", "Starting", "cyan"),
    "running": ("◼", "#", "Running", "cyan"),
    "blocked": ("⊘", "!", "Blocked", "yellow"),
    "paused": ("Ⅱ", "||", "Paused", "yellow"),
    "decision_needed": ("?", "?", "Decision needed", "magenta"),
    "verifying": ("◇", "[v]", "Verifying", "blue"),
    "merging": ("⇄", "<>", "Merging", "blue"),
    "done": ("✔", "+", "Done", "green"),
    "failed": ("✖", "x", "Failed", "red"),
    "canceled": ("—", "-", "Canceled", "default"),
    "unknown": ("!", "!", "Unknown", "yellow"),
}
STATUS_ORDER = (
    "In Progress",
    "Todo",
    "On Hold",
    "In Review",
    "Backlog",
    "In Dev",
    "In Staging",
    "Done",
    "Canceled",
    "Duplicate",
    "Pending intake",
)


@dataclass(frozen=True)
class TaskRow:
    id: str
    identifier: str
    title: str
    body: str
    status: str
    activity: str
    dependencies: tuple[str, ...]
    sync: str

    def symbol(self, ascii=False):
        return ACTIVITIES[self.activity][1 if ascii else 0]


@dataclass(frozen=True)
class Snapshot:
    tasks: tuple[TaskRow, ...]
    available: bool = True
    quarantined_count: int = 0

    def groups(self):
        groups = {}
        for task in self.tasks:
            groups.setdefault(task.status, []).append(task)
        return [(status, groups[status]) for status in STATUS_ORDER if status in groups] + [
            (status, rows) for status, rows in groups.items() if status not in STATUS_ORDER
        ]


def activity(data):
    # Values come only from the shared scoped projection, never task prose.
    if data.get("sync_state") != "confirmed" or data.get("execution_state") in {
        "stopping_or_unresolved",
        "stale_projection",
    }:
        return "unknown"
    if data.get("execution_state") == "idle":
        if data.get("observed_status") == "Done":
            return "done"
        if data.get("observed_status") in {"Canceled", "Duplicate"}:
            return "canceled"
    if data.get("scheduling", {}).get("state") == "unknown":
        return "unknown"
    explicit = data.get("activity")
    if explicit is not None:
        return explicit if explicit in ACTIVITIES else "unknown"
    state = data.get("execution_state")
    if state in ACTIVITIES:
        return state
    if state == "idle":
        if data.get("observed_status") == "Done":
            return "done"
        if data.get("observed_status") in {"Canceled", "Duplicate"}:
            return "canceled"
        if data.get("queued_once") and data.get("observed_status") == "Todo":
            return "queued"
    return "unknown"


class SessionReader:
    def __init__(self, service):
        self.service = service
        self.binding = (service.session_id, service.scope)

    def snapshot(self):
        try:
            if (self.service.session_id, self.service.scope) != self.binding:
                return Snapshot((), available=False)
            envelope = self.service.call("task_list", {})
            if not envelope.get("ok") or not isinstance(envelope.get("data"), list):
                return Snapshot((), available=False)
            rows, identities = [], set()
            for data in envelope["data"]:
                if data.get("quarantined") or data.get("active_group") is False:
                    continue
                identity = data["id"]
                if not isinstance(identity, str) or identity in identities:
                    return Snapshot((), available=False)
                identities.add(identity)
                blockers = data.get("scheduling", {}).get("blockers", [])
                rows.append(
                    TaskRow(
                        identity,
                        literal(data.get("display_identifier") or "local:" + identity),
                        literal(data["title"]),
                        literal(data.get("body"), multiline=True),
                        literal(data.get("observed_status") or "Pending intake"),
                        activity(data),
                        tuple(str(b["dependency"]) for b in blockers if b.get("dependency")),
                        str(data.get("sync_state", "unknown")),
                    )
                )
            return Snapshot(tuple(rows), quarantined_count=envelope.get("quarantined_count", 0))
        except Exception:
            # A lost authority/read clears old rows, including formerly owned prose.
            return Snapshot((), available=False)


def status_line(snapshot, *, ascii=False):
    if not snapshot.available:
        return "Tasks: unavailable"
    counts = Counter(task.activity for task in snapshot.tasks)
    fragments = [f"Tasks {len(snapshot.tasks)}"]
    for key in ACTIVITIES:
        if counts[key]:
            symbol = ACTIVITIES[key][1 if ascii else 0]
            fragments.append(f"{symbol} {ACTIVITIES[key][2]} {counts[key]}")
    pending = sum(task.sync != "confirmed" for task in snapshot.tasks)
    if pending:
        fragments.append(f"Sync pending {pending}")
    return " | ".join(fragments)
