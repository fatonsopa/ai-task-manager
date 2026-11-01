"""Owned-group signals require the original waitable leader and matching identity."""

import subprocess
import sys

from tools.build_support.process_group import OwnedProcessGroup


def test_changed_start_identity_prevents_signals_and_retains_uncertainty():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(20)"], start_new_session=True
    )
    group = OwnedProcessGroup(process)
    initial = group.capture()
    assert initial.known
    original_identity = group.start_identity
    try:
        group.start_identity = "a different process birth"
        stopped = group.stop()
        assert not stopped.confirmed
        assert stopped.signals == ()
        assert stopped.observation.reason == "leader_start_identity_changed"
        assert process.poll() is None
    finally:
        group.start_identity = original_identity
        stopped = group.stop()
        assert stopped.confirmed
        group.reap_exited_leader()


def test_reaped_leader_cannot_authorize_a_later_group_signal(monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    group = OwnedProcessGroup(process)
    group.capture()
    process.wait(timeout=2)
    sent = []
    monkeypatch.setattr(
        "tools.build_support.process_group.os.killpg", lambda *arguments: sent.append(arguments)
    )
    stopped = group.stop()
    assert not stopped.confirmed
    assert stopped.observation.reason == "leader_already_reaped"
    assert stopped.signals == ()
    assert sent == []
