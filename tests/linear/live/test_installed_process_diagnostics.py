"""Authored process observations; no provider, receiver or endurance acceptance."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.process.groups import (
    GroupObservation,
    LeaderObservation,
    OwnedProcessGroup,
    ProcessMember,
)

from . import installed_endpoint
from .installed_endpoint import InstalledReceiverProcess
from .ledger import DriverRefused


def observer(tmp_path, leader, group):
    tmp_path.chmod(0o700)
    value = object.__new__(InstalledReceiverProcess)
    value.argv = ("authored-private-command",)
    value.config_file = tmp_path / "service.json"
    value.group = SimpleNamespace(
        process=SimpleNamespace(pid=12345, args=value.argv),
        group_id=12345,
        leader=lambda: leader,
        observe=lambda: group,
    )
    # Isolate the process-classification boundary; package admission is unchanged.
    value._package = lambda: None
    return value


@pytest.mark.parametrize(
    "leader,group,classification,code",
    [
        (
            LeaderObservation(False, reason="leader_already_reaped"),
            GroupObservation(False, reason="leader_already_reaped"),
            "leader_observation_unknown",
            "installed_receiver_process_observation_unknown",
        ),
        (
            LeaderObservation(True, True, 0),
            GroupObservation(True, (ProcessMember(12345, 12345, "Z", "authored"),)),
            "leader_exited",
            "installed_receiver_process_not_running",
        ),
        (
            LeaderObservation(True),
            GroupObservation(False, reason="process_inventory_unavailable"),
            "group_observation_unknown",
            "installed_receiver_process_observation_unknown",
        ),
        (
            LeaderObservation(True),
            GroupObservation(True, (ProcessMember(12345, 12345, "Z", "authored"),)),
            "owned_leader_not_live",
            "installed_receiver_process_not_running",
        ),
    ],
)
def test_process_refusal_retains_exact_branch(tmp_path, leader, group, classification, code):
    value = observer(tmp_path, leader, group)
    with pytest.raises(DriverRefused, match=code) as caught:
        value._process()
    record = json.loads((tmp_path / "receiver-process-refusal-12345.json").read_text())
    assert record["classification"] == classification
    assert record["leader"]["known"] is leader.known
    assert record["leader"]["exited"] is (leader.exited if leader.known else None)
    assert record["group"]["known"] is group.known
    if not group.known:
        assert record["group"]["owned_leader_present"] is None
        assert record["group"]["owned_leader_live"] is None
    assert record["pid"] == record["group_id"] == 12345
    assert caught.value.category == code
    assert record["recorded_at"] > 0
    assert (tmp_path / "receiver-process-refusal-12345.json").stat().st_mode & 0o777 == 0o600


def test_unknown_inventory_does_not_establish_owned_child_exit(tmp_path, monkeypatch):
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    owned = OwnedProcessGroup(child)
    try:
        assert owned.capture().known
        value = observer(tmp_path, LeaderObservation(True), GroupObservation(True))
        value.group = owned
        value.argv = tuple(child.args)
        with monkeypatch.context() as patch:
            patch.setattr(
                owned,
                "observe",
                lambda: GroupObservation(False, reason="process_inventory_unavailable"),
            )
            with pytest.raises(DriverRefused, match="process_observation_unknown"):
                value._process()
        assert child.poll() is None
        assert owned.leader().known and not owned.leader().exited
        record = json.loads(
            (tmp_path / f"receiver-process-refusal-{child.pid}.json").read_text()
        )
        assert record["classification"] == "group_observation_unknown"
        assert record["leader"] == {"known": True, "exited": False, "reason": None}
    finally:
        assert owned.stop().confirmed
        owned.reap_exited_leader()


@pytest.mark.parametrize("reason", ["authored-secret-must-not-be-recorded", {"secret": "private"}])
def test_process_diagnostic_excludes_unrecognized_text_and_command(tmp_path, reason):
    secret = "authored-secret-must-not-be-recorded"
    value = observer(tmp_path, LeaderObservation(False, reason=reason), GroupObservation(False))
    with pytest.raises(DriverRefused, match="process_observation_unknown"):
        value._process()
    raw = (tmp_path / "receiver-process-refusal-12345.json").read_text()
    assert secret not in raw and value.argv[0] not in raw
    assert json.loads(raw)["leader"]["reason"] == "unrecognized"


def test_first_process_refusal_is_retained_without_overwrite(tmp_path):
    value = observer(tmp_path, LeaderObservation(False), GroupObservation(False))
    path = tmp_path / "receiver-process-refusal-12345.json"
    with pytest.raises(DriverRefused, match="process_observation_unknown"):
        value._process()
    original = path.read_bytes()
    value.group.leader = lambda: LeaderObservation(True, True, 0)
    with pytest.raises(DriverRefused, match="process_not_running") as caught:
        value._process()
    assert caught.value.diagnostic_retention_failed is True
    assert path.read_bytes() == original


def test_process_refusal_remains_closed_if_diagnostic_cannot_be_saved(tmp_path, monkeypatch):
    value = observer(tmp_path, LeaderObservation(False), GroupObservation(False))

    def refuse(*args, **kwargs):
        raise OSError("authored-untrusted-error-text")

    monkeypatch.setattr(installed_endpoint, "write_private", refuse)
    with pytest.raises(DriverRefused, match="process_observation_unknown") as caught:
        value._process()
    assert caught.value.diagnostic_retention_failed is True
    assert "authored-untrusted-error-text" not in str(caught.value)


def test_healthy_process_observation_adds_no_diagnostic(tmp_path):
    value = observer(
        tmp_path,
        LeaderObservation(True),
        GroupObservation(True, (ProcessMember(12345, 12345, "S", "authored"),)),
    )
    value._process()
    assert list(tmp_path.iterdir()) == []
