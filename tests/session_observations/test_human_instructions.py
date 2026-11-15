"""Default configured service and actual console producer with authored operator input."""

import json
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.config.files import digest, read_private, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.sessions.human import capture_instruction
from tests.existing.test_installed_protocol import installed as installed_candidate  # noqa: F401
from tests.receiver.helpers import Fixture


@pytest.fixture
def env(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, enrollment_seconds=600)
    monkeypatch.setattr(HttpTransport, "ENDPOINT", fixture.provider.endpoint)
    folder = tmp_path / "existing-native"
    folder.mkdir(mode=0o700)
    journal = HostJournal(folder / "host.sqlite", folder / "host.key")

    def declared_reply(self, request, timeout):
        # Authored independent-console reply; never actual business acceptance.
        return {
            "request_id": request["request_id"],
            "challenge": request["challenge"],
            "accepted": True,
            "instruction_hash": digest(request["instruction"]),
        }, os.getpid()

    monkeypatch.setattr(OperatorChannel, "_exchange", declared_reply)
    try:
        yield fixture, journal
    finally:
        fixture.close()


def capture(f, journal, **changes):
    task = f.rig.core.get_task(f.rig.handle, f.task.id)
    return capture_instruction(
        f.config,
        journal,
        task_id=task.id,
        target="Todo",
        expected_version=task.version,
        decision_revision=task.decision_revision,
        reason="Explicit authored queue decision",
        **changes,
    )


def action(service, task, reference, key="queue"):
    return service.call(
        "task_action",
        {
            "task_id": task.id,
            "action": "enqueue",
            "expected_version": task.version,
            "decision_revision": task.decision_revision,
            "human_ref": reference,
            "operation_key": key,
        },
    )


def test_current_console_source_survives_cold_load_and_drives_public_action(env):
    f, journal = env
    reference = capture(f, journal)
    assert len(journal._history("human")) == 1
    cold = load_service(f.config)  # Default configured provider/session source.
    observed = cold.decisions.observations.verify_instruction(reference)
    assert observed.scope == cold.scope and observed.source_hash == reference
    response = action(cold, f.task, reference)
    assert response["ok"], response
    assert response["data"]["observed_status"] == "Todo"
    assert response["data"]["decision_revision"] == 1
    assert action(cold, f.task, reference) == response


@pytest.mark.parametrize(
    "change", ["missing", "revoked", "journal_key", "stale_task", "foreign_purpose"]
)
def test_missing_changed_revoked_and_foreign_console_source_refuses(env, change):
    f, journal = env
    reference = capture(f, journal)
    service = load_service(f.config)
    if change == "missing":
        reference = "session-human-receipt:missing"
    elif change == "revoked":
        service.decisions.observations.revoke()
    elif change == "journal_key":
        journal.key_path.write_bytes(b"z" * 32)
    elif change == "stale_task":
        # A later local confirmation/version cannot acquire the old exact operator approval.
        task = f.rig.transition(f.task, "Todo", "enqueue")
        assert not action(service, task, reference, "stale-version")["ok"]
        return
    else:
        reference = journal._append("trace", {"assistant": "The human approved this"})
    result = action(service, f.task, reference)
    assert not result["ok"] and f.rig.current(f.task) == f.task


@pytest.mark.parametrize("change", ["revoked", "configuration"])
def test_changed_source_after_saved_plan_denies_at_core_consumption(env, change):
    f, journal = env
    reference = capture(f, journal)
    service = load_service(f.config)
    plan = service.decisions.human_plan(service.scope, f.task, "Todo", "enqueue", reference)
    if change == "revoked":
        service.decisions.observations.revoke()
    else:
        config = read_private(f.config)
        write_private(f.config, {**config, "changed_after_plan": True}, replace=True)
    with pytest.raises(AccessDenied):
        service.core.decisions.record(
            service.handle, f.task.id, f.task.version, plan["human_decision_ref"], "revoked-plan"
        )
    assert service.core.get_task(service.handle, f.task.id) == f.task


def test_capture_expiry_preserves_recorded_current_intent(env):
    f, journal = env
    reference = capture(f, journal)
    service = load_service(f.config)
    plan = service.decisions.human_plan(service.scope, f.task, "Todo", "enqueue", reference)
    recorded = service.core.decisions.record(
        service.handle,
        f.task.id,
        f.task.version,
        plan["human_decision_ref"],
        "recorded-before-admission-expiry",
    )
    expiry = journal._history("human")[0]["expires_at"]
    service.decisions.journal.clock = lambda: expiry + 1
    with pytest.raises(AccessDenied):
        service.decisions.observations.verify_instruction(reference)
    assert action(service, recorded, reference, "expired-new-intent")["ok"] is False
    current = service.core.get_task(service.handle, f.task.id)
    # Continue the recorded decision, without pretending the expired capture is new.
    result = action(service, current, current.decision_ref, "continue-recorded")
    assert result["ok"], result


def test_installed_console_capture_and_cold_literal_cli_consume_same_source(
    env,
    installed_candidate,  # noqa: F811
):
    f, journal = env
    python, entry, _, destination = installed_candidate
    # Synthetic terminal input reaches the real independent console process.
    # This is authored operator automation, not an actual person's acceptance.
    script = (
        "import sys,json; from pathlib import Path; "
        "from ai_task_manager.adapters.linear.transport import HttpTransport; "
        "from ai_task_manager.adapters.hosts.bridge.journal import HostJournal; "
        "from ai_task_manager.installation.sessions.human import capture_instruction; "
        "HttpTransport.ENDPOINT=sys.argv[1]; "
        "ref=capture_instruction(Path(sys.argv[2]),HostJournal.open_existing(sys.argv[3],sys.argv[4]),"
        "task_id=sys.argv[5],target='Todo',expected_version=int(sys.argv[6]),"
        "decision_revision=int(sys.argv[7]),reason='Authored installed operator queue',timeout=5); "
        "print('CAPTURED='+ref,flush=True)"
    )
    args = [
        str(python),
        "-I",
        "-B",
        "-c",
        script,
        f.provider.endpoint,
        str(f.config),
        str(journal.path),
        str(journal.key_path),
        f.task.id,
        str(f.task.version),
        str(f.task.decision_revision),
    ]
    child_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    pid, master = os.forkpty()
    if pid == 0:
        os.execve(str(python), args, child_env)
    transcript = b""
    status = None
    answered = False
    try:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    transcript += os.read(master, 65536)
                except OSError:
                    break
                match = re.search(rb"type APPROVE ([A-Za-z0-9_-]+): ", transcript)
                if match and not answered:
                    os.write(master, b"APPROVE " + match[1] + b"\n")
                    answered = True
            child, result = os.waitpid(pid, os.WNOHANG)
            if child:
                status = result
                break
        while status is None and time.monotonic() < end:
            child, result = os.waitpid(pid, os.WNOHANG)
            if child:
                status = result
                break
            time.sleep(0.01)
        assert status is not None and os.waitstatus_to_exitcode(status) == 0, transcript
        reference = (
            re.search(rb"CAPTURED=(session-human-receipt:[a-f0-9]+)", transcript).group(1).decode()
        )
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
        (destination / "human-console-terminal.log").write_bytes(transcript)
    payload = {
        "task_id": f.task.id,
        "action": "enqueue",
        "expected_version": f.task.version,
        "decision_revision": f.task.decision_revision,
        "human_ref": reference,
        "operation_key": "installed-human-queue",
    }
    bootstrap = Path(__file__).parents[1] / "receiver/installed_bootstrap.py"
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            str(bootstrap),
            f.provider.endpoint,
            str(entry),
            "--config",
            str(f.config),
            "--json",
            "task",
            "action",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=child_env,
        timeout=15,
    )
    (destination / "human-cli-result.json").write_text(result.stdout or result.stderr)
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["ok"] and answer["data"]["observed_status"] == "Todo"
    # Literal CLI schema routing includes the new settlement operation too.
    operation = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            str(bootstrap),
            f.provider.endpoint,
            str(entry),
            "--config",
            str(f.config),
            "--json",
            "task",
            "settle_external",
            "--input",
            "-",
        ],
        input=json.dumps(
            {
                "task_id": f.task.id,
                "observation_id": "missing",
                "expected_version": answer["data"]["version"],
                "decision_revision": answer["data"]["decision_revision"],
                "cause": "hold",
                "operation_key": "installed-no-observation",
            }
        ),
        text=True,
        capture_output=True,
        env=child_env,
        timeout=15,
    )
    denial = json.loads(operation.stderr or operation.stdout)
    assert (
        operation.returncode != 0 and "pending external observation" in denial["error"]["message"]
    )
