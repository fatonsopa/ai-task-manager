"""Owned tmux companion layout; view controls receive only resize capability."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.config.files import write_private

from .pane_control import binding_digest, process_identity
from .process_watch import ProcessWatch


class LayoutUnavailable(RuntimeError):
    pass


class ResizeOnly:
    def __init__(self, owner):
        self._owner = owner

    def resize(self, hidden):
        self._owner.resize(hidden)


class OwnedLayout:
    """Trusted caller owns supplied processes; closing a view never calls close()."""

    def __init__(self, binary, private_root, *, environment=None):
        self.binary, self.root = str(Path(binary).resolve()), Path(private_root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.socket_root = Path(tempfile.mkdtemp(prefix="aitm-view-", dir="/tmp"))
        self.socket = self.socket_root / "server"
        self.socket_identity = None
        self.config = self.root / "tmux.conf"
        self.config.write_text(
            "set -g status off\nset -g mouse on\nset -g pane-border-status off\n"
            "set -g default-terminal screen-256color\nset -s escape-time 0\n"
        )
        os.chmod(self.config, 0o600)
        self.environment = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            **(environment or {}),
        }
        self.process = self.group = None
        self.host_pane = self.sidebar_pane = None
        self.preferred_width = 40
        self.closed = None
        self.last_panes = []
        self.process_watch = None
        self.close_history = []

    def _command(self, *arguments):
        result = subprocess.run(
            [self.binary, "-S", str(self.socket), *arguments],
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode:
            raise LayoutUnavailable("Owned terminal layout command failed")
        return result.stdout.strip()

    def create(self, host_argv, sidebar_argv, *, columns=120, rows=30, sidebar_width=40):
        if self.process is not None:
            raise LayoutUnavailable("Terminal layout already started")
        if not 20 <= columns <= 1000 or not 5 <= rows <= 300 or not 8 <= sidebar_width <= 100:
            raise LayoutUnavailable("Invalid terminal dimensions")
        if any(
            not argv or any(not isinstance(a, str) or "\0" in a for a in argv)
            for argv in (host_argv, sidebar_argv)
        ):
            raise LayoutUnavailable("Explicit terminal process arguments required")
        self.preferred_width = sidebar_width
        self.output = (self.root / "server.log").open("xb")
        os.chmod(self.root / "server.log", 0o600)
        try:
            self.process = subprocess.Popen(
                [self.binary, "-D", "-S", str(self.socket), "-f", str(self.config)],
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=self.output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.group = OwnedProcessGroup(self.process)
            self.group.capture()
            self.process_watch = ProcessWatch(self.process.pid)
            self.process_watch.observe()
            until = time.monotonic() + 3
            while not self.socket.exists():
                leader = self.group.leader()
                if not leader.known or leader.exited or time.monotonic() >= until:
                    raise LayoutUnavailable("Owned terminal server did not start")
                time.sleep(0.01)
            info = self.socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise LayoutUnavailable("Owned terminal socket required")
            self.socket_identity = (info.st_dev, info.st_ino)
            self.host_pane = self._command(
                "new-session",
                "-d",
                "-x",
                str(columns),
                "-y",
                str(rows),
                "-s",
                "aitm",
                "-P",
                "-F",
                "#{pane_id}",
                "--",
                *host_argv,
            )
            self.process_watch.observe()
            width = min(sidebar_width, max(8, columns - 32))
            self.sidebar_pane = self._command(
                "split-window",
                "-h",
                "-t",
                self.host_pane,
                "-l",
                str(width),
                "-P",
                "-F",
                "#{pane_id}",
                "--",
                *sidebar_argv,
            )
            if not all(
                re.fullmatch(r"%[0-9]+", pane) for pane in (self.host_pane, self.sidebar_pane)
            ):
                raise LayoutUnavailable("Owned terminal pane identity missing")
            self._command("select-pane", "-t", self.host_pane)
            self.last_panes = self.panes()
            self.process_watch.observe()
            return ResizeOnly(self)
        except BaseException:
            self.close()
            raise

    def _check(self):
        if self.group is None or not self.group.leader().known or self.group.leader().exited:
            raise LayoutUnavailable("Owned terminal server is unavailable")
        info = self.socket.lstat()
        if (info.st_dev, info.st_ino) != self.socket_identity or info.st_uid != os.getuid():
            raise LayoutUnavailable("Owned terminal socket changed")

    def panes(self, *, complete=True):
        self._check()
        rows = []
        for line in self._command(
            "list-panes",
            "-t",
            "aitm",
            "-F",
            "#{pane_id} #{pane_pid} #{pane_left} #{pane_top} #{pane_width} "
            "#{pane_height} #{window_width} #{window_height}",
        ).splitlines():
            identity, *values = line.split()
            if not re.fullmatch(r"%[0-9]+", identity) or len(values) != 7:
                raise LayoutUnavailable("Terminal pane observation unavailable")
            numbers = [int(value) for value in values]
            rows.append(
                dict(
                    zip(
                        (
                            "id",
                            "pid",
                            "left",
                            "top",
                            "width",
                            "height",
                            "window_width",
                            "window_height",
                        ),
                        (identity, *numbers),
                        strict=True,
                    )
                )
            )
        found = {row["id"] for row in rows}
        owned = {self.host_pane, self.sidebar_pane}
        if (complete and found != owned) or not found <= owned:
            raise LayoutUnavailable("Owned terminal panes changed")
        return rows

    def write_context(self, service):
        self.panes()
        value = {
            "version": 1,
            "binding": binding_digest(service),
            "binary": self.binary,
            "binary_sha256": hashlib.sha256(Path(self.binary).read_bytes()).hexdigest(),
            "socket": str(self.socket),
            "socket_identity": list(self.socket_identity),
            "server_pid": self.process.pid,
            "server_start_identity": process_identity(self.process.pid),
            "session": "aitm",
            "host_pane": self.host_pane,
            "sidebar_pane": self.sidebar_pane,
            "sidebar_width": self.preferred_width,
        }
        path = self.root / "context.json"
        if path.exists():
            raise LayoutUnavailable("Terminal context already exists")
        write_private(path, value)
        return path

    def resize(self, hidden):
        panes = self.panes()
        width = panes[0]["window_width"]
        desired = 8 if hidden else min(self.preferred_width, max(8, width - 32))
        self._command("resize-pane", "-t", self.sidebar_pane, "-x", str(desired))

    def capture(self, pane):
        self.panes(complete=False)
        if pane not in {self.host_pane, self.sidebar_pane}:
            raise LayoutUnavailable("Foreign terminal pane refused")
        return self._command("capture-pane", "-p", "-t", pane)

    def close(self):
        if self.closed is not None and self.closed.get("resources_stopped"):
            return self.closed
        if self.process_watch:
            self.process_watch.observe()
        if self.group is not None and self.process.returncode is None:
            stop = self.group.stop()
            evidence = self.group.evidence(stop, orphaned=False)
            if stop.confirmed:
                self.group.reap_exited_leader()
        elif self.closed is not None:
            evidence = self.closed["server"]
        else:
            evidence = {"confirmed_stop": self.process is None}
        if hasattr(self, "output"):
            self.output.close()
        pane_observations = []
        pane_pids = [row["pid"] for row in self.last_panes]
        panes_stopped = not pane_pids
        deadline = time.monotonic() + 1
        while pane_pids:
            try:
                reply = subprocess.run(
                    ["ps", "-p", ",".join(map(str, pane_pids)), "-o", "pid=,stat="],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                rows = [line.split() for line in reply.stdout.splitlines()]
                known = reply.returncode in {0, 1} and all(len(row) == 2 for row in rows)
                live = (
                    [int(row[0]) for row in rows if not row[1].startswith(("Z", "X"))]
                    if known
                    else None
                )
            except (OSError, subprocess.TimeoutExpired, ValueError):
                rows, known, live = [], False, None
            pane_observations.append({"known": known, "rows": rows, "live_pids": live})
            panes_stopped = known and not live
            if panes_stopped or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        descendant = {"known": True, "live": []}
        if self.process_watch:
            deadline = time.monotonic() + 1
            while True:
                descendant = self.process_watch.observe()
                if descendant["known"] and not descendant["live"] or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        self.closed = {
            "server": evidence,
            "returncode": self.process.returncode if self.process else None,
            "pane_identity": [self.host_pane, self.sidebar_pane],
            "last_observed_panes": self.last_panes,
            "panes_stopped": panes_stopped,
            "pane_stop_observations": pane_observations,
            "observed_processes": descendant,
            "resources_stopped": bool(
                evidence["confirmed_stop"]
                and panes_stopped
                and descendant["known"]
                and not descendant["live"]
            ),
            "attempt": len(self.close_history) + 1,
        }
        self.close_history.append(self.closed)
        (self.root / f"close-attempt-{len(self.close_history)}.json").write_text(
            json.dumps(self.closed, indent=2, sort_keys=True) + "\n"
        )
        (self.root / "close.json").write_text(
            json.dumps(self.closed, indent=2, sort_keys=True) + "\n"
        )
        if self.closed["resources_stopped"]:
            shutil.rmtree(self.socket_root)
        return self.closed
