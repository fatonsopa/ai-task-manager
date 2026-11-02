"""Bounded disposable PTY captures; this never invokes a model or normal host."""

import codecs
import fcntl
import json
import os
import re
import select
import struct
import subprocess
import termios
import time

import pyte

from ai_task_manager.adapters.process.groups import OwnedProcessGroup


def color_codes(raw):
    forbidden = (
        set(range(30, 38))
        | set(range(40, 48))
        | set(range(90, 98))
        | set(range(100, 108))
        | {38, 48, 58}
    )
    return [
        int(value)
        for group in re.findall(rb"\x1b\[([0-9;]*)m", raw)
        for value in group.split(b";")
        if value and int(value) in forbidden
    ]


class Terminal:
    def __init__(self, argv, folder, *, columns=44, rows=20, environment=None):
        self.folder, self.columns, self.rows = folder, columns, rows
        folder.mkdir(mode=0o700)
        (folder / "home").mkdir(mode=0o700)
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        env = {
            "HOME": str(folder / "home"),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": ".:src",
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            **(environment or {}),
        }
        self.process = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
        )
        os.close(slave)
        self.group = OwnedProcessGroup(self.process)
        self.group.capture()
        self.screen = pyte.Screen(columns, rows)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.raw = bytearray()
        self.frames = []
        self.closed = None

    def pump(self, seconds=0.1):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            ready, _, _ = select.select(
                [self.master], [], [], min(0.05, max(0, until - time.monotonic()))
            )
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.raw.extend(data)
                self.stream.feed(self.decoder.decode(data))
                if b"\x1b[6n" in data:
                    os.write(self.master, b"\x1b[1;1R")
        return "\n".join(self.screen.display)

    def wait(self, predicate, *, seconds=5, label="observed"):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            display = self.pump()
            if predicate(display):
                self.frame(label)
                return display
        self.frame(label + "-failed")
        raise AssertionError("Owned PTY condition not observed: " + label)

    def frame(self, label):
        self.frames.append(
            {
                "label": label,
                "monotonic": time.monotonic(),
                "columns": self.columns,
                "rows": self.rows,
                "display": list(self.screen.display),
                "bold_cells": [
                    [column for column, cell in self.screen.buffer[row].items() if cell.bold]
                    for row in range(self.rows)
                ],
            }
        )
        self.save()

    def send(self, data):
        os.write(self.master, data)

    def resize(self, columns, rows):
        self.columns, self.rows = columns, rows
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.screen.resize(lines=rows, columns=columns)
        os.kill(self.process.pid, __import__("signal").SIGWINCH)

    def save(self):
        (self.folder / "wire.ansi").write_bytes(self.raw)
        (self.folder / "frames.json").write_text(
            json.dumps(self.frames, indent=2, ensure_ascii=False) + "\n"
        )

    def close(self):
        if self.closed is not None:
            return self.closed
        self.frame("before-stop")
        attempts = []
        deadline = time.monotonic() + 1
        for _ in range(3):
            stop = self.group.stop()
            evidence = self.group.evidence(stop, orphaned=False)
            attempts.append(evidence)
            if (
                stop.confirmed
                or stop.observation.reason != "leader_exit_race_unconfirmed"
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.02)
        if stop.confirmed:
            self.group.reap_exited_leader()
        os.close(self.master)
        self.closed = {
            "pid": self.process.pid,
            "returncode": self.process.returncode,
            "group": evidence,
            "stop_attempts": attempts,
            "master_closed": True,
        }
        (self.folder / "stop.json").write_text(
            json.dumps(self.closed, indent=2, sort_keys=True) + "\n"
        )
        assert evidence["confirmed_stop"] and self.process.returncode is not None
        return self.closed
