"""Actual installed CLI with a labeled synthetic provider transport substitution."""

import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

import pytest

from ai_task_manager.config.files import write_private
from tests.linear.helpers import STATES

from .helpers import ROUTE, Fixture, request, wait_for


@pytest.fixture
def fixture(tmp_path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


def command(fixture, *arguments):
    base = Path(os.environ["AITM_RECEIVER_TEST_VENV"])
    return [
        str(base / "bin/python"),
        "-I",
        str(Path(__file__).with_name("installed_bootstrap.py")),
        fixture.provider.endpoint,
        str(base / "bin/aitm"),
        "--config",
        str(fixture.config),
        "--json",
        *arguments,
    ]


def start(fixture):
    process = subprocess.Popen(
        command(fixture, "sync", "listen"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        assert selector.select(15), "Installed CLI readiness deadline"
        ready = json.loads(process.stdout.readline())
        assert ready["ok"] and ready["event"] == "receiver_ready", ready
        return process, ready["data"]
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        raise
    finally:
        selector.close()


def stop(fixture, process, *, signal_stop=True, label="stop"):
    if signal_stop and process.poll() is None:
        process.send_signal(signal.SIGTERM)
    output, error = process.communicate(timeout=15)
    record = {
        "pid": process.pid,
        "returncode": process.returncode,
        "stdout": output,
        "stderr": error,
        "waited": True,
    }
    (fixture.root / (label + ".json")).write_text(json.dumps(record, indent=2))
    assert not error
    final = json.loads(output.splitlines()[-1])
    assert final["event"] == "receiver_stopped" and final["cleanup"]["stopped"]
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)
    return final


def query(fixture):
    result = subprocess.run(
        command(fixture, "task", "list"), capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["data"][0]


def test_ir02_ir03_ir06_actual_installed_command_and_same_task_interface(fixture):
    process, ready = start(fixture)
    address = (ready["host"], ready["port"])
    try:
        duplicate = subprocess.run(
            command(fixture, "sync", "listen"), capture_output=True, text=True, timeout=15
        )
        assert duplicate.returncode == 1
        assert request(address, ready["health_route"], method="GET")[0] == 200
        headers, body = fixture.event()
        wrong = [(k, "0" * 64 if k == "Linear-Signature" else v) for k, v in headers]
        assert request(address, ROUTE, wrong, body)[0] == 401
        assert request(address, ROUTE + "/foreign", headers, body)[0] == 404
        fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
        headers, body = fixture.event()
        assert request(address, ROUTE, headers, body)[0] == 200
        wait_for(
            lambda: (
                fixture.rig.core.get_task(fixture.rig.handle, fixture.task.id).observed_status
                == "Todo"
            )
        )
        observed = query(fixture)
        assert observed["id"] == fixture.task.id and observed["observed_status"] == "Todo"
        assert request(address, ROUTE, headers, body)[0] == 200
        time.sleep(0.1)
        assert query(fixture)["version"] == observed["version"]
        (fixture.root / "observed-task.json").write_text(json.dumps(observed, indent=2))
    finally:
        assert stop(fixture, process)["ok"]
    restarted, _ = start(fixture)
    assert stop(fixture, restarted, label="restart-stop")["ok"]
    assert query(fixture)["version"] == observed["version"]


def test_ir05_actual_installed_restart_repairs_partial_durable_intake(fixture):
    fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    headers, body = fixture.event()
    fixture.rig.journal.accept_delivery(
        fixture.rig.scope, dict(headers)["Linear-Delivery"], body, json.loads(body)
    )
    process, _ = start(fixture)
    try:
        wait_for(
            lambda: (
                fixture.rig.core.get_task(fixture.rig.handle, fixture.task.id).observed_status
                == "Todo"
            )
        )
        assert query(fixture)["observed_status"] == "Todo"
    finally:
        assert stop(fixture, process)["ok"]


def test_ir07_actual_installed_configuration_change_stops_admission(fixture):
    process, _ = start(fixture)
    try:
        fixture.receiver.write_bytes(fixture.receiver.read_bytes() + b"\n")
        final = stop(fixture, process, signal_stop=False)
        assert not final["ok"] and process.returncode == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_ir07_actual_installed_finite_lifetime_stops_cleanly(fixture):
    write_private(fixture.receiver, {**fixture.values, "lifetime_seconds": 0.5}, replace=True)
    process, _ = start(fixture)
    assert stop(fixture, process, signal_stop=False)["ok"]


def test_ir07_actual_installed_enrollment_expiry_stops_without_renewal(tmp_path):
    fixture = Fixture(tmp_path, enrollment_seconds=3)
    process = None
    try:
        process, _ = start(fixture)
        final = stop(fixture, process, signal_stop=False)
        assert not final["ok"] and process.returncode == 1
        assert fixture.rig.core.session_binding(fixture.rig.handle)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        fixture.close()


@pytest.mark.parametrize("parent_mode", [0o755, 0o700])
def test_installed_private_database_parent_startup_diagnostic(tmp_path, parent_mode):
    # Match the live topology: private service/authority files and a separate
    # registered database directory. No real credentials or provider calls.
    fixture = Fixture(tmp_path / "service", database_directory=tmp_path / "registered-database")
    try:
        fixture.rig.db_path.parent.chmod(parent_mode)
        if parent_mode == 0o700:
            process, ready = start(fixture)
            assert ready["host"] == "127.0.0.1"
            assert stop(fixture, process, label="private-parent-stop")["ok"]
            return
        result = subprocess.run(
            command(fixture, "sync", "listen"), capture_output=True, text=True, timeout=15
        )
        (fixture.root / "public-parent-refusal.json").write_text(
            json.dumps(
                {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            )
        )
        assert result.returncode == 1 and not result.stderr
        final = json.loads(result.stdout)
        assert final["event"] == "receiver_stopped" and final["cleanup"]["stopped"]
        assert final["diagnostics"][0]["stage"] == "receiver_start"
        cause = final["diagnostics"][0]["causes"][0]
        assert cause["code"] == "access_denied"
        assert cause["frames"][-1]["module"] == "ai_task_manager.application.linear_sync.listener"
        assert cause["frames"][-1]["function"] == "__init__"
        assert str(fixture.root) not in result.stdout
        assert "Receiver database requires" not in result.stdout
    finally:
        fixture.close()
