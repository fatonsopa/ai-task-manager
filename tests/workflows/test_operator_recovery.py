"""Current installed recovery with explicitly authored provider/native observations."""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.application.workflows.service import WorkflowService
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.workflows.recovery import owner_state, request
from ai_task_manager.storage.workflows import WorkflowStore
from tests.workflows.helpers import WorkflowFixture

pytest_plugins = [
    "tests.workflow_installation.test_installation", "tests.workflow_control.test_public"
]


@pytest.fixture
def wf(tmp_path):
    value = WorkflowFixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


def stock_cli(fixture, action, data, *, installation=True):
    root, _, _ = fixture
    venv = Path(os.environ["AITM_OPERATOR_TEST_VENV"])
    command = [str(venv / "bin/aitm"), "--config", str(root / "service.json"), "--json"]
    if installation:
        command += ["--installation", str(root / "installation.json")]
    command += ["workflow", action, "--input", "-"]
    started_at = time.monotonic()
    outcome = subprocess.run(command, input=json.dumps(data), capture_output=True, text=True,
                             timeout=30, env={"PATH": os.defpath})
    report = os.environ.get("WORKFLOW_ENTRYPOINT_REPORT")
    if report:
        with Path(report).open("a") as stream:
            stream.write(json.dumps({
                "kind": "operator_cli", "command": command, "request": data,
                "returncode": outcome.returncode, "stdout": outcome.stdout,
                "stderr": outcome.stderr, "elapsed_seconds": time.monotonic() - started_at,
            }) + "\n")
    return outcome


def started(fixture):
    root, _, _ = fixture
    if (root / "recovery-intent.json").exists():
        intent = json.loads((root / "recovery-intent.json").read_text())
        return dict(intent, action="inspect", operation_key="recovery-original"), None
    ready = json.loads((root / "prepared.json").read_text())
    first = stock_cli(fixture, "run", {"task_id": ready["task_id"],
                      "plan_ref": ready["plan_ref"], "operation_key": "recovery-original"},
                      installation=False)
    envelope = json.loads(first.stdout or first.stderr)
    assert first.returncode == 4 and envelope["data"]["state"] == "awaiting_review", envelope
    return {"action": "inspect", "workflow_ref": envelope["data"]["workflow"],
            "operation_key": "recovery-original"}, envelope


def counts(root):
    runtime = root / "installed-state/runtime.sqlite"
    if not runtime.exists():
        runtime = root / "runtime.sqlite"
    with sqlite3.connect(runtime) as db:
        return (db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM runtime_effects").fetchone()[0])


@pytest.mark.parametrize("owned", [("recovery",)], indirect=True)
def test_operator_installed_inspect_and_original_resume(owned):
    installed = owned["root"], owned["package"], owned["child"]
    root, _, owner = installed
    value, first = started(installed)
    before = counts(root)
    observed = stock_cli(installed, "recover", value)
    result = json.loads(observed.stdout or observed.stderr)
    assert observed.returncode == 0 and result["data"]["owner_state"] == "live", result
    assert result["data"]["workflow"] == value["workflow_ref"]
    assert result["data"]["operation_key"] == "recovery-original"
    actions = result["data"]["action_availability"]
    assert set(actions) == {"inspect", "resume", "stop", "successor", "retire"}
    assert actions["inspect"] == {"callable": True, "effect_eligibility": "no_effects"}
    assert actions["resume"] == {
        "callable": True, "effect_eligibility": "requires_workflow_guards"
    }
    assert all(actions[name] == {"callable": False, "blocker": "not_installed"}
               for name in ("stop", "successor", "retire"))
    assert counts(root) == before and owner.poll() is None
    value["action"] = "resume"
    resumed = stock_cli(installed, "recover", value)
    again = json.loads(resumed.stdout or resumed.stderr)
    assert resumed.returncode == 4 and again["data"] == first["data"], again
    assert counts(root) == before and before[0] == 1


@pytest.mark.parametrize("installed", ["recovery"], indirect=True)
@pytest.mark.parametrize("change", ["key", "foreign", "copy", "rewrite", "missing", "mode"])
def test_operator_installed_refuses_changed_or_unsupported_inputs(installed, change):
    root, _, owner = installed
    value, _ = started(installed)
    before = counts(root)
    restore = None
    if change == "key":
        value["operation_key"] = "invented-key"
    elif change == "foreign":
        value["workflow_ref"] = "workflow:foreign"
    elif change == "copy":
        target = root / "copy.json"
        shutil.copyfile(root / "installation.json", target)
        target.chmod(0o600)
        # Even changing its self-reported path cannot reproduce signed activation.
        data = json.loads(target.read_text())
        data["installation_path"] = str(target)
        target.write_text(json.dumps(data))
        restore = (root / "installation.json").read_bytes()
        (root / "installation.json").write_bytes(target.read_bytes())
    elif change == "rewrite":
        p = root / "installation.json"
        restore = p.read_bytes()
        data = json.loads(restore)
        data["deadline"] += 100
        p.write_text(json.dumps(data))
    elif change == "missing":
        p = root / "installed-state/workflow.sqlite"
        p.rename(p.with_suffix(".retained"))
    elif change == "mode":
        value["action"] = "successor"
    failed = stock_cli(installed, "recover", value)
    result = json.loads(failed.stdout or failed.stderr)
    assert failed.returncode in {3, 4, 6} and not result["ok"], result
    assert counts(root) == before and owner.poll() is None
    if restore is not None:
        (root / "installation.json").write_bytes(restore)
    if change == "missing":
        p = root / "installed-state/workflow.sqlite"
        assert not p.exists(), "Recovery must not recreate missing history"
        p.with_suffix(".retained").rename(p)


def test_existing_workflow_inspection_never_starts_or_changes_history(wf):
    service = WorkflowService(wf.driver, {wf.task.id: wf.plan.id})
    before = wf.store.read(wf.ref, wf.rig.scope)
    observed = service.inspect(wf.ref)["data"]
    assert observed["operation_key"] == "workflow-intake"
    assert observed["attempt_id"] is None and observed["steps"] == {}
    assert observed["action_availability"]["inspect"]["callable"] is True
    assert observed["action_availability"]["resume"] == {
        "callable": True, "effect_eligibility": "requires_workflow_guards"
    }
    assert observed["action_availability"]["successor"] == {
        "callable": False, "blocker": "not_installed"
    }
    assert wf.store.read(wf.ref, wf.rig.scope) == before and not wf.runtime.dispatches
    with pytest.raises(AccessDenied):
        service.inspect("workflow:foreign")


def test_existing_workflow_opening_rejects_missing_replaced_and_wrong_uuid(wf):
    original = wf.store.path
    assert WorkflowStore.open_existing(original, store_id=wf.store.store_id).read(
        wf.ref, wf.rig.scope)["id"] == wf.ref
    with pytest.raises(AccessDenied):
        WorkflowStore.open_existing(original, store_id="3fbc942b-571c-418f-91a4-a0c61e477dcb")
    missing = original.with_name("missing.sqlite")
    with pytest.raises((OSError, AccessDenied)):
        WorkflowStore.open_existing(missing, store_id=wf.store.store_id)
    assert not missing.exists()


@pytest.mark.parametrize("value", [None, {}, {"action": "inspect", "workflow_ref": "w",
                          "operation_key": "k", "authority": "invented"},
                          {"action": "arbitrary", "workflow_ref": "w", "operation_key": "k"}])
def test_closed_operator_request_cannot_supply_authority(value):
    with pytest.raises(AccessDenied):
        request(value)


def test_owner_observation_distinguishes_dead_reused_and_unknown(monkeypatch):
    from ai_task_manager.installation.workflows import recovery

    descriptor = {"pid": 12345, "start": "original"}
    activation = {"owner": descriptor}
    for code, out, err, expected in [(1, "", "", "dead"),
                                   (2, "", "observer failed", "unknown"),
                                   (0, "0 replacement", "", "reused")]:
        monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
            [], code, stdout=out, stderr=err))
        assert owner_state(descriptor, activation) == expected


@pytest.mark.parametrize("change", ["expired", "foreign", "revoked"])
def test_shared_inspection_requires_current_enrollment_before_workflow_access(tmp_path, change):
    from tests.application.helpers import ServiceRig

    fixture = ServiceRig(tmp_path)
    service = fixture.service
    grant = fixture.observations.grants["host-grant"]
    if change == "expired":
        fixture.observations.grants["host-grant"] = replace(
            grant, expires_at=service.core.context.clock() - 1)
    elif change == "foreign":
        fixture.observations.grants["host-grant"] = replace(
            grant, scope=replace(grant.scope, logical_session="foreign"))
    else:
        fixture.observations.grants.clear()
    # No workflow port is installed: denial must precede access to that port.
    outcome = service.call("workflow_inspect", {"workflow_ref": "workflow:unread"})
    assert not outcome["ok"] and outcome["outcome"] == "denied", outcome
    invalid = service.call("workflow_inspect", {"workflow_ref": "workflow:unread",
                                               "authority": {"approved": True}})
    assert invalid["exit_code"] == 2
