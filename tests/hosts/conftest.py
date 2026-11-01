import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.adapters.hosts.bridge.transcript import OwnedTranscript
from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.domains.work.models import Scope
from ai_task_manager.supervisor.models import Command


@pytest.fixture
def journal(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return HostJournal(root / "host.sqlite", root / "host.key")


@pytest.fixture
def binding():
    return HostBinding(
        Scope("linear", "workspace", "principal", "project", "logical"),
        "core-session",
        "attempt",
        "worker",
        "codex",
        "principal-ref",
        "a" * 64,
        time.time() + 30,
    )


@dataclass
class FixturePolicy:
    configuration_digest: str
    cwd: Path
    environment: dict
    allowed_tools: tuple = ()

    def check(self):
        return None


@pytest.fixture
def native(tmp_path, journal, binding):
    processes = []

    def create(frames=(), program=None, *, deadline=None):
        output = tmp_path / f"output-{len(processes)}"
        output.mkdir(mode=0o700)
        if program is None:
            program = (
                "import sys,json,time\n"
                "for line in sys.stdin:\n"
                " data=json.loads(line)\n"
                ' if data.get("op")=="emit":\n'
                '  print(json.dumps(data["frame"]),flush=True)\n'
                ' elif data.get("op")=="echo": print(line.strip(),flush=True)\n'
            )
        command = Command(
            (sys.executable, "-I", "-u", "-c", program), tmp_path, {"PATH": os.defpath}, 10
        )
        process = HostProcess(command, output, deadline=deadline or time.time() + 10)
        process.start()
        processes.append(process)
        transcript = OwnedTranscript(process, journal, binding)

        def emit(frame):
            process.write(json.dumps({"op": "emit", "frame": frame}) + "\n")
            return transcript.read(1)

        for frame in frames:
            emit(frame)
        return process, transcript, emit

    yield create
    for process in processes:
        if not process._closed:
            process.stop()
