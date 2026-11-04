"""Finite duration controls; authored child only, no real ngrok or credentials."""

import hashlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.tunnels import ngrok

from .test_ngrok_transport import TARGET, wait_observation
from .test_ngrok_transport import owned as owned


def prepare(tmp_path, **kwargs):
    executable = Path(sys.executable).resolve()
    return ngrok.NgrokTunnel(
        executable,
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        tmp_path / "credential-not-read.env",
        TARGET,
        tmp_path / "owned-run",
        **kwargs,
    )


def test_nd01_omitted_duration_keeps_105_second_default(tmp_path):
    tunnel = prepare(tmp_path)
    assert tunnel.lifetime == 105.0
    assert tunnel.process is None and not tunnel.launch_attempted
    assert not tunnel.credential_file.exists()


@pytest.mark.parametrize(
    "lifetime", [105.0001, 106, 420, 86400], ids=["fractional", "integer", "session", "upperbound"]
)
def test_nd02_explicit_finite_duration_preserves_requested_value(tmp_path, lifetime):
    tunnel = prepare(tmp_path, lifetime=lifetime)
    assert tunnel.lifetime == lifetime
    assert tunnel.process is None and not tunnel.launch_attempted
    assert not tunnel.credential_file.exists()


@pytest.mark.parametrize(
    "lifetime",
    [True, False, 0, -1, 86400.0001, 86401, float("inf"), -float("inf"), float("nan"), "420", None],
)
def test_nd03_invalid_duration_refuses_before_files_or_launch(tmp_path, lifetime):
    with pytest.raises(ngrok.NgrokRefused, match="^ngrok_bound_required$"):
        prepare(tmp_path, lifetime=lifetime)
    assert not (tmp_path / "owned-run").exists()
    assert not (tmp_path / "credential-not-read.env").exists()


def test_nd04_long_duration_retains_one_deadline_and_owned_cleanup(owned, monkeypatch):
    offset = 0
    # Replace only the adapter's clock binding; process supervision keeps real time.
    monkeypatch.setattr(ngrok, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset))
    start, _ = owned
    before = time.monotonic()
    tunnel = start(lifetime=420)
    after = time.monotonic()
    deadline = tunnel.deadline
    assert before + 420 <= deadline <= after + 420
    wait_observation(tunnel)
    offset = 106
    assert tunnel.observe(TARGET)["pid"] == tunnel.process.pid
    assert tunnel.deadline == deadline
    offset = 421
    tunnel.thread.join(3)
    assert not tunnel.thread.is_alive()
    assert tunnel.closed["confirmed_stop"] and tunnel.closed["reaped"]
    assert tunnel.deadline == deadline
    with pytest.raises(ngrok.NgrokRefused, match="deadline"):
        tunnel.observe(TARGET)
    with pytest.raises(ngrok.NgrokRefused, match="start_once"):
        tunnel.start()
