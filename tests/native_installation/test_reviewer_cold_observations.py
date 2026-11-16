"""Forward reviewer ownership with actual processes and an authored authority guard."""

import os
from pathlib import Path

import pytest

import ai_task_manager.installation.native as native
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.workflow_control.identity import process_start
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.workflows import InstalledWorkflow
from ai_task_manager.installation.workflows.cold import ColdRecovery
from ai_task_manager.supervisor.models import RuntimeUncertain
from tests.native_installation import test_reviewer_composition

installed = test_reviewer_composition.installed


@pytest.mark.parametrize("change", [None, "missing_key", "unknown_group"])
def test_cold_reviewer_observation_requires_original_stopped_components(
    installed, monkeypatch, change
):
    s = installed
    owner = InstalledWorkflow(
        s["config"],
        s["primary"].installation,
        native.resolve(s["config"], s["primary"].installation),
    )
    s["owners"].append(owner)
    producer = owner.registry.reviewer_factory(s["inspection"])

    def interrupted(*args, **kwargs):
        raise RuntimeError("authored reviewer interruption")

    monkeypatch.setattr(producer.workers, "execute_reviewed", interrupted)
    with pytest.raises(RuntimeError, match="authored reviewer"):
        owner._review_source(Path(owner.spec.data["state_root"])).resolve(s["inspection"])
    parent = HostJournal.open_existing(owner.host_journal.path, owner.host_journal.key_path)
    traces = [r["payload"] for r in parent._history("trace")]
    assert any(r.get("effect") == "reviewer_state_created" for r in traces)
    recovery = ColdRecovery.__new__(ColdRecovery)
    recovery.spec, recovery.config_file = owner.spec, s["config"]
    recovery.witness = {"owner": {"pid": os.getpid(), "start": process_start(os.getpid())}}
    recovery.reference = "authored-current-stop"
    recovery.check = lambda: None
    key = producer.workers.hosts.journal.key_path
    retained = key.with_suffix(".retained")
    if change == "missing_key":
        key.rename(retained)
    elif change == "unknown_group":
        from ai_task_manager.adapters.process import recovery as process_recovery
        from ai_task_manager.adapters.process.groups import GroupObservation

        monkeypatch.setattr(
            process_recovery,
            "observe_recorded_group",
            lambda *_: GroupObservation(False, reason="authored unknown"),
        )
    try:
        if change is None:
            recovery._related_stopped(owner.service, traces)
        else:
            with pytest.raises((AccessDenied, EvidenceRequired, RuntimeUncertain)):
                recovery._related_stopped(owner.service, traces)
    finally:
        if retained.exists():
            retained.rename(key)
