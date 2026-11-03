"""Interrupted ownership and explicit credential contracts with synthetic local children."""

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.adapters.tunnels import ngrok

from .ledger import DriverRefused
from .ngrok_transport import run, source_pins
from .test_ngrok_transport import TARGET, TOKEN, event, private, wait_observation

NAME = "AITM_CREDENTIAL_Ngrok_Test"


def prepared(tmp_path, *, environment=False, **kwargs):
    executable = tmp_path / "authored-child"
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import os, signal, time\n"
        f"assert os.environ['NGROK_AUTHTOKEN'] == {TOKEN!r}\n"
        "assert 'HOME' not in os.environ\n"
        "assert set(os.environ) <= {'NGROK_AUTHTOKEN','PATH','LC_ALL','LC_CTYPE',"
        "'__CF_USER_TEXT_ENCODING'}\n"
        "assert not signal.pthread_sigmask(signal.SIG_BLOCK, [])\n"
        f"print({json.dumps(event())!r}, flush=True)\n"
        "time.sleep(8)\n"
    )
    executable.chmod(0o700)
    source = (
        None if environment else private(tmp_path / "synthetic.env", f"NGROK_AUTH_TOKEN={TOKEN}\n")
    )
    return ngrok.NgrokTunnel(
        executable,
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        source,
        TARGET,
        tmp_path / "run",
        lifetime=2,
        credential_environment=NAME if environment else None,
        **kwargs,
    )


def settle(tunnel, tmp_path):
    """Retain proof and stop only the fixture's original child even after a failed oracle."""
    result = tunnel.stop()
    if tunnel.process is not None and tunnel.process.returncode is None:
        owner = OwnedProcessGroup(tunnel.process)
        stop = owner.stop()
        owner.reap_exited_leader()
        assert stop.confirmed and tunnel.process.returncode is not None
    (tmp_path / "cleanup.json").write_text(json.dumps(result, indent=2))
    return result


@pytest.mark.parametrize("boundary", ["popen_return", "group_constructor"])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGALRM])
def test_ng13_registered_signal_retains_and_reaps_spawned_child(
    tmp_path, monkeypatch, boundary, signum
):
    tunnel = prepared(tmp_path)
    old = signal.getsignal(signum)
    real = ngrok.subprocess.Popen if boundary == "popen_return" else ngrok.OwnedProcessGroup
    captured = []

    def interrupt(signum, frame):
        raise DriverRefused("transport_interrupted")

    def at_boundary(*args, **kwargs):
        result = real(*args, **kwargs)
        captured.append(result if boundary == "popen_return" else args[0])
        os.kill(os.getpid(), signum)
        return result

    signal.signal(signum, interrupt)
    try:
        with monkeypatch.context() as patch:
            if boundary == "popen_return":
                patch.setattr(ngrok.subprocess, "Popen", at_boundary)
                # Group monitoring uses Popen too; inject only for the actual tunnel argv.
                patch.setattr(ngrok, "require_group_monitoring", lambda: None)

                def selected(*args, **kwargs):
                    return (
                        at_boundary(*args, **kwargs)
                        if args[0] == tunnel.argv
                        else real(*args, **kwargs)
                    )

                patch.setattr(ngrok.subprocess, "Popen", selected)
            else:
                patch.setattr(ngrok, "OwnedProcessGroup", at_boundary)
            with pytest.raises(DriverRefused, match="transport_interrupted"):
                tunnel.start()
        assert len(captured) == 1
        assert tunnel.process is captured[0] and tunnel.group.process is captured[0]
        stopped = tunnel.stop()
        assert stopped["confirmed_stop"] and stopped["reaped"]
        assert tunnel.process.returncode is not None
        assert signal.getsignal(signum) is interrupt
        with pytest.raises(ProcessLookupError):
            os.kill(tunnel.process.pid, 0)
    finally:
        signal.signal(signum, old)
        settle(tunnel, tmp_path)


@pytest.mark.parametrize(
    "boundary",
    ["group_constructor", "capture", "thread_constructor", "thread_start", "thread_started"],
)
def test_ng14_setup_failure_stops_retained_child(tmp_path, monkeypatch, boundary):
    tunnel = prepared(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("authored setup failure")

    try:
        with monkeypatch.context() as patch:
            if boundary == "group_constructor":
                real = ngrok.OwnedProcessGroup
                calls = 0

                def fail_once(process):
                    nonlocal calls
                    calls += 1
                    return fail() if calls == 1 else real(process)

                patch.setattr(ngrok, "OwnedProcessGroup", fail_once)
            elif boundary == "capture":
                patch.setattr(ngrok.OwnedProcessGroup, "capture", fail)
            elif boundary == "thread_constructor":
                patch.setattr(ngrok.threading, "Thread", fail)
            elif boundary == "thread_started":
                real = ngrok.threading.Thread.start

                def start_then_fail(thread):
                    real(thread)
                    fail()

                patch.setattr(ngrok.threading.Thread, "start", start_then_fail)
            else:
                patch.setattr(ngrok.threading.Thread, "start", fail)
            with pytest.raises(RuntimeError, match="authored setup failure"):
                tunnel.start()
        stopped = tunnel.stop()
        assert stopped["confirmed_stop"] and stopped["reaped"]
        assert tunnel.process.returncode is not None
    finally:
        settle(tunnel, tmp_path)


def test_ng14_uncertain_spawn_never_claims_no_child(tmp_path, monkeypatch):
    tunnel = prepared(tmp_path)
    real = ngrok.subprocess.Popen

    def refuse(*args, **kwargs):
        if args[0] == tunnel.argv:
            raise OSError("authored unavailable spawn")
        return real(*args, **kwargs)

    monkeypatch.setattr(ngrok.subprocess, "Popen", refuse)
    with pytest.raises(OSError):
        tunnel.start()
    result = tunnel.stop()
    assert not result["confirmed_stop"] and result["reason"] == "ngrok_cleanup_unknown"
    assert tunnel.process is None


def test_ng14_reader_failure_stops_owned_child_only(tmp_path, monkeypatch):
    tunnel = prepared(tmp_path)
    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(8)"], start_new_session=True
    )

    def refuse(*args, **kwargs):
        raise OSError("authored reader failure")

    try:
        monkeypatch.setattr(ngrok.select, "select", refuse)
        tunnel.start()
        tunnel.thread.join(4)
        assert not tunnel.thread.is_alive()
        assert tunnel.stop()["confirmed_stop"] and tunnel.process.returncode is not None
        assert foreign.poll() is None
    finally:
        settle(tunnel, tmp_path)
        foreign.terminate()
        foreign.wait(timeout=2)


def test_ng15_selected_environment_isolated_and_bound(tmp_path, monkeypatch):
    monkeypatch.setenv(NAME, TOKEN)
    monkeypatch.setenv("NGROK_AUTHTOKEN", "wrong-ambient")
    monkeypatch.setenv("NGROK_AUTH_TOKEN", "another-ambient")
    monkeypatch.setenv("HTTPS_PROXY", "must-not-propagate")
    tunnel = prepared(tmp_path, environment=True)
    try:
        tunnel.start()
        assert wait_observation(tunnel)["origin"] == event()["url"]
        assert not any(
            TOKEN.encode() in p.read_bytes() for p in tunnel.root.rglob("*") if p.is_file()
        )
        monkeypatch.setenv(NAME, "changed-selected")
        with pytest.raises(ngrok.NgrokRefused, match="credential_changed"):
            tunnel.observe(TARGET)
    finally:
        assert settle(tunnel, tmp_path)["confirmed_stop"]


@pytest.mark.parametrize("value", [None, "", "two words", "x\n", "é", "x" * 4097])
def test_ng15_invalid_selected_value_never_falls_back(tmp_path, monkeypatch, value):
    monkeypatch.setenv("NGROK_AUTHTOKEN", TOKEN)
    monkeypatch.delenv(NAME, raising=False)
    if value is not None:
        monkeypatch.setenv(NAME, value)
    tunnel = prepared(tmp_path, environment=True)
    with pytest.raises(ngrok.NgrokRefused, match="credential_absent_or_ambiguous"):
        tunnel.start()
    assert tunnel.process is None and not tunnel.launch_attempted
    assert tunnel.stop()["confirmed_stop"]


@pytest.mark.parametrize(
    "name", ["HOME", "PATH", "NGROK_AUTHTOKEN", "AITM_CREDENTIAL_", "AITM_CREDENTIAL_é"]
)
def test_ng15_invalid_environment_name_refuses_before_files(tmp_path, name):
    with pytest.raises(ngrok.NgrokRefused, match="environment_name_refused"):
        ngrok.NgrokTunnel(
            "unread", "0" * 64, None, TARGET, tmp_path / "run", credential_environment=name
        )
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("file, name", [(None, None), ("unread", NAME)])
def test_ng15_exactly_one_credential_source_required(tmp_path, file, name):
    with pytest.raises(ngrok.NgrokRefused, match="exact_credential_source"):
        ngrok.NgrokTunnel(
            "unread", "0" * 64, file, TARGET, tmp_path / "run", credential_environment=name
        )
    assert not (tmp_path / "run").exists()


def test_ng13_interruption_during_cleanup_waits_for_owned_stop(tmp_path, monkeypatch):
    tunnel = prepared(tmp_path)
    old = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        raise DriverRefused("transport_interrupted")

    try:
        tunnel.start()
        wait_observation(tunnel)
        real = tunnel.group.stop

        def interrupt_stop():
            os.kill(os.getpid(), signal.SIGTERM)
            return real()

        signal.signal(signal.SIGTERM, interrupt)
        with monkeypatch.context() as patch:
            patch.setattr(tunnel.group, "stop", interrupt_stop)
            with pytest.raises(DriverRefused, match="transport_interrupted"):
                tunnel.stop()
        assert tunnel.closed["confirmed_stop"] and tunnel.closed["reaped"]
        assert tunnel.process.returncode is not None
    finally:
        signal.signal(signal.SIGTERM, old)
        settle(tunnel, tmp_path)


@pytest.mark.parametrize(
    "selection", [{}, {"credential_file": "unread", "credential_environment": NAME}]
)
def test_ng15_runner_rejects_missing_or_conflicting_selection_before_effects(tmp_path, selection):
    packet = {
        "run_id": "unused",
        "scope": {},
        "webhook_id": "unused",
        "root": str(tmp_path / "run"),
        "executable": "unread",
        "executable_sha256": "0" * 64,
        "source_sha256": source_pins(),
        **selection,
    }
    with pytest.raises(DriverRefused, match="transport_packet_fields_required"):
        run(packet)
    assert not (tmp_path / "run").exists()
