"""A private, scope-bound capability can resize only its owned sidebar pane."""

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.config.files import digest


def binding_digest(service):
    return digest({"session_id": service.session_id, "scope": asdict(service.scope)})


def process_identity(pid):
    reply = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=,lstart="],
        capture_output=True,
        text=True,
        timeout=2,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    fields = reply.stdout.strip().split(None, 1)
    if reply.returncode or len(fields) != 2 or int(fields[0]) != os.getuid():
        raise ValueError("Owned terminal server is unavailable")
    return " ".join(fields[1].split())


class PaneControl:
    def __init__(self, context_file, service):
        path = Path(context_file)
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_size > 16384
        ):
            raise ValueError("Private terminal layout context required")
        self.context = json.loads(path.read_text())
        self.service = service
        if self.context.get("version") != 1 or self.context.get("binding") != binding_digest(
            service
        ):
            raise ValueError("Terminal layout belongs to another session")
        self._check()

    def _check(self):
        context = self.context
        if context["binding"] != binding_digest(self.service):
            raise ValueError("Terminal session binding changed")
        binary = Path(context["binary"])
        if hashlib.sha256(binary.read_bytes()).hexdigest() != context["binary_sha256"]:
            raise ValueError("Terminal layout executable changed")
        if process_identity(context["server_pid"]) != context["server_start_identity"]:
            raise ValueError("Terminal server identity changed")
        info = Path(context["socket"]).lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or [info.st_dev, info.st_ino] != context["socket_identity"]
        ):
            raise ValueError("Terminal socket identity changed")

    def resize(self, hidden):
        if type(hidden) is not bool:
            raise ValueError("Terminal visibility must be a boolean")
        self._check()
        context = self.context
        command = [context["binary"], "-S", context["socket"]]
        reply = subprocess.run(
            [*command, "list-panes", "-t", context["session"], "-F", "#{pane_id} #{window_width}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        rows = [line.split() for line in reply.stdout.splitlines()]
        if (
            reply.returncode
            or len(rows) != 2
            or any(len(row) != 2 for row in rows)
            or {row[0] for row in rows} != {context["host_pane"], context["sidebar_pane"]}
            or not all(re.fullmatch(r"%[0-9]+", row[0]) and row[1].isdecimal() for row in rows)
        ):
            raise ValueError("Owned terminal panes changed")
        width = int(rows[0][1])
        desired = 8 if hidden else min(context["sidebar_width"], max(8, width - 32))
        reply = subprocess.run(
            [*command, "resize-pane", "-t", context["sidebar_pane"], "-x", str(desired)],
            capture_output=True,
            timeout=3,
        )
        if reply.returncode:
            raise ValueError("Owned terminal resize failed")
