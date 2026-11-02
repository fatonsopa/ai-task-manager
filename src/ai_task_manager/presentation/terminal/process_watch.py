"""Observe known terminal process identities; never signal a guessed descendant."""

import subprocess


def inventory():
    reply = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
        capture_output=True,
        text=True,
        timeout=2,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if reply.returncode or not reply.stdout.strip():
        return None
    rows = {}
    try:
        for line in reply.stdout.splitlines():
            pid, parent, group, state, start = line.split(None, 4)
            rows[int(pid)] = {
                "pid": int(pid),
                "parent": int(parent),
                "group": int(group),
                "state": state,
                "start_identity": " ".join(start.split()),
            }
    except (ValueError, TypeError):
        return None
    return rows


class ProcessWatch:
    def __init__(self, server_pid):
        self.server_pid = server_pid
        self.identities = {}
        self.observations = []

    def observe(self):
        rows = inventory()
        if rows is None:
            value = {"known": False, "live": None, "reason": "process_inventory_unavailable"}
        else:
            if not self.identities and self.server_pid in rows:
                self.identities[self.server_pid] = rows[self.server_pid]
            current = {
                pid
                for pid, old in self.identities.items()
                if pid in rows and rows[pid]["start_identity"] == old["start_identity"]
            }
            while True:
                discovered = {pid for pid, row in rows.items() if row["parent"] in current}
                additions = discovered - current
                if not additions:
                    break
                for pid in additions:
                    self.identities[pid] = rows[pid]
                current.update(additions)
            live = [rows[pid] for pid in current if not rows[pid]["state"].startswith(("Z", "X"))]
            value = {
                "known": bool(self.identities),
                "live": live,
                "identities": list(self.identities.values()),
                "scope": "observed server/pane process tree; not atomic arbitrary-fork attestation",
            }
        self.observations.append(value)
        return value
