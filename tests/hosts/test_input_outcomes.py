"""Durable bounded observations and original-error retention, using owned fixtures."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.invocations import (
    InvocationBinding,
    InvocationPermits,
    spawn_claimed,
)
from ai_task_manager.adapters.hosts.transports import input_audit
from ai_task_manager.adapters.hosts.transports.input_audit import InputAudit
from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import Command, RuntimeUncertain
from tests.hosts.test_physical_invocations import allowed
from tests.hosts.test_physical_invocations import pipe as pipe
from tests.test_build_control_effect_boundaries import running_invocation
from tests.test_build_control_policy_amendment import rig as rig


def test_input_outcome_is_durable_only_after_boundary_exit(pipe, tmp_path):
    read_fd, writer = pipe
    audit = InputAudit(tmp_path)
    inside = []

    @contextmanager
    def boundary():
        inside.append(True)
        try:
            yield
        finally:
            inside.pop()

    def observe(outcome):
        assert not inside
        audit.record(outcome, "owned-call")

    outcome = writer.send(
        '{"private":"fixture-value-never-recorded"}\n', boundary=boundary, observe=observe
    )
    assert json.loads(os.read(read_fd, 4096))["private"] == "fixture-value-never-recorded"
    data = json.loads(audit.path.read_text())
    assert data["call_id"] == "owned-call"
    assert data["outcome"]["written_frames"] == data["outcome"]["confirmed_boundaries"] == 1
    assert not outcome.uncertain
    assert "fixture-value-never-recorded" not in audit.path.read_text()
    assert audit.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("variant", ["overflow", "symlink", "fsync_failure"])
def test_required_audit_failure_quarantines_before_later_input(
    pipe, tmp_path, monkeypatch, variant
):
    read_fd, writer = pipe
    audit = InputAudit(tmp_path)
    target = tmp_path / "unchanged-owned-target"
    target.write_bytes(b"sentinel")
    if variant == "overflow":
        audit.path.write_bytes(b"x" * input_audit.MAX_AUDIT_BYTES)
        audit.path.chmod(0o600)
    elif variant == "symlink":
        audit.path.symlink_to(target)
    else:

        def failed(fd):
            raise OSError("Authored fsync failure")

        monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises((AccessDenied, OSError)):
        writer.send(
            '{"once":true}\n',
            boundary=allowed,
            observe=lambda outcome: audit.record(outcome, "owned-call"),
        )
    assert audit.failed and writer.quarantined
    assert writer.last_outcome.written_frames == 1
    assert writer.last_outcome.confirmed_boundaries == 1
    assert writer.last_outcome.uncertain and writer.last_outcome.phase == "audit"
    with pytest.raises(AccessDenied, match="never resend"):
        writer.send('{"twice":true}\n', boundary=allowed)
    assert os.read(read_fd, 4096) == b'{"once":true}\n'
    assert target.read_bytes() == b"sentinel"


def test_original_input_refusal_survives_audit_and_cleanup_failures(pipe, tmp_path, monkeypatch):
    read_fd, writer = pipe
    command = Command((sys.executable, "-I", "-c", "pass"), tmp_path, {"PATH": os.defpath}, 2)
    process = HostProcess(command, tmp_path / "output", deadline=time.time() + 2)
    process.native_input = writer
    primary = AccessDenied("Authored original current-boundary refusal")

    @contextmanager
    def refused():
        raise primary
        yield

    def stop_failed():
        raise RuntimeUncertain("Authored missing stop observation")

    def audit_failed(outcome, call_id):
        raise OSError("Authored required journal failure")

    monkeypatch.setattr(process, "verify_identity", lambda: None)
    monkeypatch.setattr(process, "_input_boundary", refused)
    monkeypatch.setattr(process, "stop", stop_failed)
    monkeypatch.setattr(process.input_audit, "record", audit_failed)
    with pytest.raises(AccessDenied) as caught:
        process.write('{"must_not_complete":true}\n')
    assert caught.value is primary
    assert primary.input_audit_error == "OSError"
    assert primary.owned_stop_error == "RuntimeUncertain"
    assert not hasattr(primary, "owned_stop_evidence")
    writer.close()
    with pytest.raises(json.JSONDecodeError):
        json.loads(os.read(read_fd, 4096))


@pytest.mark.parametrize("variant", ["confirmed_cleanup", "cleanup_failure"])
def test_post_popen_callback_failure_retains_original_and_owned_group(rig, monkeypatch, variant):
    control = running_invocation(rig, start=False)
    snapshot = control.snapshot()
    binding = InvocationBinding.observed(
        {"run_id": snapshot["run_id"], "policy_revision": 1, "call": snapshot["calls"][0]}
    )
    issuer = InvocationPermits(control, {"call": binding})
    permit = issuer.prepare_launch("call")
    original_launch = issuer._launch
    primary = RuntimeError("Authored callback failed after actual Popen")

    @contextmanager
    def launch(prepared):
        with original_launch(prepared):

            def report(**fields):
                raise primary

            yield report

    original_stop = OwnedProcessGroup.stop
    with monkeypatch.context() as change:
        change.setattr(issuer, "_launch", launch)
        if variant == "cleanup_failure":

            def failed(group):
                raise RuntimeUncertain("Authored stop observation unavailable")

            change.setattr(OwnedProcessGroup, "stop", failed)
        with pytest.raises(RuntimeError) as caught:
            spawn_claimed(
                permit,
                [sys.executable, "-I", "-c", "import time;time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    assert caught.value is primary and primary.owned_group is not None
    if variant == "cleanup_failure":
        assert primary.owned_stop_error == "RuntimeUncertain"
        assert not hasattr(primary, "owned_stop_evidence")
        # Separate fixture recovery through the exact retained ownership anchor;
        # this does not replace the original unknown product stop observation.
        stopped = original_stop(primary.owned_group)
        group = primary.owned_group.evidence(stopped, orphaned=False)
        if stopped.confirmed:
            primary.owned_group.reap_exited_leader()
    else:
        group = primary.owned_stop_evidence
    directory = Path(os.environ["AITM_TEST_EVIDENCE_DIR"])
    (directory / f"build-effect-groups-callback-{variant}.json").write_text(
        json.dumps(
            {
                "group": group,
                "product_stop_was_unknown": variant == "cleanup_failure",
            }
        )
    )
    assert group["confirmed_stop"]
    call = control.snapshot()["calls"][0]
    assert call["status"] == "launching" and call["worker_state"] != "released"


def test_actual_root_rollback_cannot_erase_complete_frame_write(rig, pipe, tmp_path, monkeypatch):
    read_fd, writer = pipe
    control = running_invocation(rig, start=False)
    snapshot = control.snapshot()
    binding = InvocationBinding.observed(
        {"run_id": snapshot["run_id"], "policy_revision": 1, "call": snapshot["calls"][0]}
    )
    permit = InvocationPermits(control, {"call": binding}).prepare_launch("call")
    original = control._store.transaction
    audit = InputAudit(tmp_path)
    marker = OSError("Authored Root transaction refusal after physical frame write")

    @contextmanager
    def rollback():
        with original() as transaction:
            yield transaction
            raise marker

    @contextmanager
    def boundary():
        with permit.launch_boundary() as report:
            yield
            report(process_pid=None, process_start_identity=None)

    with monkeypatch.context() as change:
        change.setattr(control._store, "transaction", rollback)
        with pytest.raises(OSError) as caught:
            writer.send(
                '{"external_effect_possible":true}\n',
                boundary=boundary,
                observe=lambda outcome: audit.record(outcome, "call"),
            )
    assert caught.value is marker
    assert json.loads(os.read(read_fd, 4096)) == {"external_effect_possible": True}
    assert writer.quarantined
    outcome = json.loads(audit.path.read_text())["outcome"]
    assert outcome["written_frames"] == 1 and outcome["confirmed_boundaries"] == 0
    assert outcome["uncertain"] and outcome["phase"] == "boundary_exit"
    assert control.snapshot()["calls"][0]["status"] == "launching"
