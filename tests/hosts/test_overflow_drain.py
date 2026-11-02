"""Real overflow/control observations over owned credential-free relay fixtures."""

import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ai_task_manager.adapters.process.groups import OwnedProcessGroup


def test_overflow_drain_stays_bounded_when_reader_is_blocked(native):
    program = (
        "import signal,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "time.sleep(.1)\n"
        "while True: sys.stdout.buffer.write(b'x'*65536);sys.stdout.buffer.flush()\n"
    )
    process, _, _ = native(program=program)
    until = time.monotonic() + 3
    while time.monotonic() < until and not process.group.leader().exited:
        time.sleep(0.01)
    assert process.group.leader().exited, "Blocked output must not suspend the relay watchdog"
    retained = b""
    while select.select([process.process.stdout], [], [], 0)[0]:
        part = os.read(process.process.stdout.fileno(), 65536)
        if not part:
            break
        retained += part
    stopped = process.stop()
    assert stopped.confirmed
    assert b"native_stdout_limit" in (process.directory / "native.stderr").read_bytes()
    assert len(retained) <= 4194304


def test_controller_loss_preempts_observed_overflow_drain(tmp_path, request):
    root = Path(__file__).parents[2]
    marker = tmp_path / "overflow-read-observed"
    wrapper = (
        "import os,sys,pathlib\n"
        f"sys.path.insert(0,{str(root / 'src')!r})\n"
        "from ai_task_manager.adapters.hosts.transports import relay\n"
        "real_read,real_spawn=relay.os.read,relay.subprocess.Popen\n"
        "target=None\nseen=0\n"
        "def spawned(*args,**kwargs):\n"
        " global target\n child=real_spawn(*args,**kwargs)\n"
        " target=child.stdout.fileno()\n return child\n"
        "def observed(fd,size):\n"
        " global seen\n data=real_read(fd,size)\n"
        " if fd==target:\n"
        "  seen+=len(data)\n"
        f"  if seen>4194304: pathlib.Path({str(marker)!r}).write_text('actual overflow read')\n"
        " return data\n"
        "relay.os.read=observed\nrelay.subprocess.Popen=spawned\n"
        "sys.argv=['relay',*sys.argv[1:]]\nrelay.main()\n"
    )
    program = (
        "import signal,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "time.sleep(.1)\n"
        "while True: sys.stdout.buffer.write(b'x'*65536);sys.stdout.buffer.flush()\n"
    )
    command = {
        "argv": [sys.executable, "-I", "-u", "-c", program],
        "cwd": str(tmp_path),
        "environment": {"PATH": os.defpath},
    }
    ready_read, ready_write = os.pipe()
    native_read, native_write = os.pipe()
    group = process = None
    nonce = "owned-overflow-control"
    try:
        with tempfile.TemporaryFile(dir=tmp_path) as specification:
            os.fchmod(specification.fileno(), 0o600)
            specification.write(
                json.dumps({"nonce": nonce, "command": command, "policy": None}).encode()
            )
            specification.flush()
            specification.seek(0)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    wrapper,
                    str(ready_write),
                    nonce,
                    "5",
                    str(time.time() + 5),
                    str(specification.fileno()),
                    str(native_read),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmp_path,
                env={"PATH": os.defpath},
                pass_fds=(ready_write, specification.fileno(), native_read),
                start_new_session=True,
            )
        group = OwnedProcessGroup(process)
        assert group.capture().known
        os.close(ready_write)
        ready_write = -1
        os.close(native_read)
        native_read = -1
        process.stdin.write((json.dumps({"op": "launch", "nonce": nonce}) + "\n").encode())
        process.stdin.flush()
        assert select.select([ready_read], [], [], 2)[0]
        handshake = json.loads(os.read(ready_read, 4096))
        assert handshake["nonce"] == nonce
        until = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < until:
            time.sleep(0.005)
        assert marker.exists(), "The actual relay overflow read must precede controller loss"
        assert not group.leader().exited
        process.stdin.close()
        until = time.monotonic() + 1.5
        while not group.leader().exited and time.monotonic() < until:
            time.sleep(0.01)
        assert group.leader().exited
        stopped = group.stop()
        assert stopped.confirmed
        diagnostics = process.stderr.read()
        assert b"controller_disconnected" in diagnostics
        assert b"native_stdout_limit" not in diagnostics
    finally:
        for fd in (ready_read, ready_write, native_read, native_write):
            if fd >= 0:
                os.close(fd)
        if group is not None:
            stopped = group.stop()
            if stopped.confirmed:
                group.reap_exited_leader()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            assert stopped.confirmed
