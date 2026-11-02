"""Owned relay byte budgets with actual fixture processes, not models."""

import os
import select
import time

import pytest


@pytest.mark.parametrize("stream,limit", [("stdout", 4194304), ("stderr", 1048576)])
def test_native_output_overflow_keeps_a_finite_prefix_and_stops_owned_group(native, stream, limit):
    program = (
        "import sys,time,signal\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "time.sleep(.2)\n"
        "while True:\n"
        f" sys.{stream}.buffer.write(b'x'*65536);sys.{stream}.buffer.flush()\n"
    )
    process, _, _ = native(program=program)
    total, ended = 0, False
    end = time.monotonic() + 4
    while time.monotonic() < end:
        if not select.select([process.process.stdout], [], [], 0.1)[0]:
            continue
        part = os.read(process.process.stdout.fileno(), 65536)
        if not part:
            ended = True
            break
        total += len(part)
        assert total <= 4194304
    assert ended, "The relay must end the overflowing owned invocation within its finite bound"
    stopped = process.stop()
    assert stopped.confirmed
    retained = (process.directory / "native.stderr").read_bytes()
    assert len(retained) <= 1048576 + 128
    assert f"native_{stream}_limit".encode() in retained
    assert total == limit if stream == "stdout" else len(retained) >= limit
