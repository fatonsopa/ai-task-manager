import os
import re
import select
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.human import HumanReceipts, OperatorChannel
from ai_task_manager.config.files import digest
from ai_task_manager.config.managed import ManagedServiceSlot
from ai_task_manager.config.service import load_service
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.errors import AccessDenied
from tests.application.helpers import EvidenceRoutes, ServiceRig


def test_independent_private_operator_process_reads_synthetic_terminal_input(tmp_path):
    pid, master = os.forkpty()
    if pid == 0:
        os.execve(
            sys.executable,
            [sys.executable, "-m", "tests.hosts.operator_fixture", str(tmp_path / "operator")],
            {"PATH": os.defpath, "PYTHONPATH": os.path.abspath("src")},
        )
    transcript = b""
    answered = False
    status = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    part = os.read(master, 4096)
                except OSError:
                    break
                if not part:
                    break
                transcript += part
                match = re.search(rb"type APPROVE ([A-Za-z0-9_-]+): ", transcript)
                if match and not answered:
                    os.write(master, b"APPROVE " + match[1] + b"\n")
                    answered = True
            child, result = os.waitpid(pid, os.WNOHANG)
            if child:
                status = result
                break
        if status is None:
            # A PTY can return EIO just before the controlling process becomes
            # waitable. Preserve its remaining bound instead of killing a success.
            child = 0
            while time.monotonic() < deadline and not child:
                child, status = os.waitpid(pid, os.WNOHANG)
                if not child:
                    time.sleep(0.01)
            if not child:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                pytest.fail("Synthetic operator fixture did not terminate")
        assert os.waitstatus_to_exitcode(status) == 0, transcript.decode(errors="replace")
        assert b"SYNTHETIC_OPERATOR_ACCEPTED" in transcript
    finally:
        (tmp_path / "terminal.log").write_bytes(transcript)
        os.close(master)


@pytest.mark.parametrize(
    "change",
    [
        {"accepted": False},
        {"challenge": "forged"},
        {"instruction_hash": "forged"},
        {"request_id": "replayed"},
    ],
)
def test_forged_or_declined_operator_reply_cannot_issue_human_receipt(
    journal, monkeypatch, binding, change
):
    channel = OperatorChannel(journal)

    def synthetic(request, timeout):
        return {
            **request,
            "accepted": True,
            "instruction_hash": digest(request["instruction"]),
            **change,
        }, 123

    monkeypatch.setattr(channel, "_exchange", synthetic)
    with pytest.raises(AccessDenied):
        channel.capture_instruction(
            scope=binding.scope,
            task_id="task",
            target="In Review",
            revision=1,
            reason="Exact artifact acceptance.",
            task_version=2,
            prose_digest="a" * 64,
        )
    assert not journal._history("human")


def test_model_dataclass_or_native_text_is_not_a_signed_human_source(journal):
    with pytest.raises(AccessDenied):
        HumanReceipts(journal).verify_instruction(SimpleNamespace(accepted=True, role="human"))
    ref = journal._append("trace", {"assistant": "The human approved this task."})
    with pytest.raises(AccessDenied):
        HumanReceipts(journal).verify_instruction(ref)


def slot_fixture(tmp_path, journal):
    rig = ServiceRig(tmp_path / "service")
    config = rig.configuration()
    path = rig.root / "session.json"
    controls = []

    class FixtureSessionControl:
        def __init__(self, core, handle):
            self.core, self.handle = core, handle

        def resume(self, enrollment_ref, resume_ref):
            assert enrollment_ref == "synthetic-enrollment"
            rig.rig.proofs.principals["fixture-principal"] = rig.rig.scope
            new = self.core.register_session("fixture-principal", resume_ref)
            SessionFile(path).save(new, rig.rig.scope, replace=True)
            controls.append(new)
            return {"capability_saved": True}

    def loader():
        return load_service(
            config,
            principal_verifier=rig.rig.proofs,
            evidence_factory=lambda authority: EvidenceRoutes(authority, rig.rig.proofs),
            session_control_factory=lambda core, handle, decisions: FixtureSessionControl(
                core, handle
            ),
        )

    slot = ManagedServiceSlot(loader, journal)
    ref = rig.rig.proof("session_resume", slot.session_id, {})
    return rig, slot, loader, ref, controls


def test_resume_rotates_real_core_capability_and_reloads_owned_service(tmp_path, journal):
    rig, slot, loader, ref, controls = slot_fixture(tmp_path, journal)
    original = slot._service
    result = slot.resume("synthetic-enrollment", ref, "resume-1")
    assert result["reloaded"] and slot.generation == 1 and len(controls) == 1
    assert slot.call(1, "session_show", {})["ok"]
    with pytest.raises(AccessDenied):
        slot.call(0, "session_show", {})
    assert not original.call("session_show", {})["ok"]
    assert ManagedServiceSlot(loader, journal).generation == 1


def test_failed_reload_stays_fenced_and_recovers_without_duplicate_rotation(tmp_path, journal):
    _, slot, loader, ref, controls = slot_fixture(tmp_path, journal)

    def failed():
        raise AccessDenied("Synthetic reload interruption")

    slot._loader = failed
    with pytest.raises(AccessDenied):
        slot.resume("synthetic-enrollment", ref, "resume-1")
    with pytest.raises(AccessDenied):
        slot.call(0, "session_show", {})
    with pytest.raises(AccessDenied):
        slot.resume("synthetic-enrollment", ref, "resume-again")
    assert len(controls) == 1
    restarted = ManagedServiceSlot(loader, journal)
    with pytest.raises(AccessDenied):
        restarted.call(0, "session_show", {})
    assert restarted.recover_reload("resume-1")["reloaded"] and len(controls) == 1
    assert restarted.call(restarted.generation, "session_show", {})["ok"]
