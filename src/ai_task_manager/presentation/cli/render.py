"""Show issue stage, actual execution and synchronization as separate facts."""

import re

ISSUE_SYMBOLS = {
    "Backlog": ("○", "90"),
    "Todo": ("◇", "36"),
    "In Progress": ("■", "34"),
    "On Hold": ("⏸", "33"),
    "In Review": ("?", "35"),
    "In Dev": ("◆", "36"),
    "In Staging": ("▲", "35"),
    "Done": ("✔", "32"),
    "Canceled": ("✖", "31"),
    "Duplicate": ("=", "90"),
}


def safe_text(value):
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(" " if ord(c) < 32 or 127 <= ord(c) < 160 else c for c in value)


def task_lines(tasks, *, color=False, quarantined_count=0):
    lines = []
    for task in tasks:
        status = task.get("observed_status") or "Pending intake"
        symbol, code = ISSUE_SYMBOLS.get(status, ("◌", "90"))
        if color:
            symbol = f"\x1b[{code}m{symbol}\x1b[0m"
        title = safe_text(task["title"])
        identifier = (
            safe_text(task["display_identifier"])
            if task.get("display_identifier")
            else "local:" + safe_text(task["id"])
        )
        lines.append(
            f"{symbol} {identifier}  {title}\n  issue={status}; "
            f"execution={task['execution_state']}; sync={task['sync_state']}"
        )
    text = "\n".join(lines) if lines else "No active tasks in this session."
    if quarantined_count:
        text += f"\n{quarantined_count} out-of-scope task(s) await ownership reconciliation."
    return text
