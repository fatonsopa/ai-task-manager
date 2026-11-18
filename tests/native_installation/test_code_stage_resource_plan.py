"""Actual sealed stage input and canonical JSON plan, stopping at a required source guard."""

import json
import time
import subprocess
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.adapters.workflow_reviews.models import Inspection
from ai_task_manager.application.workflows.models import WorkflowPlan
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native import NativeManifest
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.review_stages import CodeStageSource
from ai_task_manager.storage.workflows import WorkflowStore
from ai_task_manager.supervisor.journal import RuntimeJournal
from tests.domains.helpers import Rig
from tests.native_installation.helpers import bundle, refresh_digest


@pytest.mark.parametrize("stage", ["code_worker", "code_candidate"])
def test_R01_current_sealed_resource_plan_reaches_required_command_source_guard(tmp_path, stage):
    rig = Rig(tmp_path / "core.sqlite")
    rig.now = time.time()
    task = rig.queued(code=True)
    native = bundle(tmp_path / "native", "claude", "claude-sonnet-5-5", task_id=task.id)
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    RuntimeJournal(root / "runtime.sqlite")
    HostJournal(root / "host.sqlite", root / "host.key")
    (root / "resources").mkdir(mode=0o700)
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
                    "-qm", "Owned sealed stage source"], check=True)
    base = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"],
                                   text=True).strip()
    plan = WorkflowPlan("owned-plan", "assignment", "claude", base,
                        ("owned.py",), ("owned.py",), rig.now + 100,
                        resource_locks=(("test", "owned-lock"),))
    specification = {
        "version": 1, "role": "worker", "installation_path": str(native.installation),
        "config": file_binding(native.config), "scope": asdict(rig.scope),
        "task_id": task.id, "prose_digest": task.prose_digest,
        "native_ref": "normal-native", "native_digest": "0" * 64,
        "native_manifest": str(native.manifest), "repository": str(repository),
        "source_head": plan.base, "state_root": str(root), "deadline": plan.deadline,
        "principal_ref": "owned-principal", "plan_ref": plan.id,
        "files": list(plan.allowed_files), "artifacts": list(plan.artifacts),
        "resource_locks": [list(pair) for pair in plan.resource_locks],
    }
    write_private(native.installation, specification, replace=True)
    native.data["installation"] = installation_intent(native.installation)
    write_private(native.manifest, native.data, replace=True)
    refresh_digest(native.installation, native.manifest, native.data)
    author = NativeManifest(native.manifest, native.config, native.installation)
    store = WorkflowStore(root / "workflow.sqlite")
    reference = store.create(rig.scope, task, plan, "owned-stage-plan")
    assert store.read(reference, rig.scope)["plan"]["resource_locks"] == [["test", "owned-lock"]]
    service = SimpleNamespace(core=rig.core, handle=rig.handle, scope=rig.scope)
    inspection = Inspection(json.dumps({"review_kind": stage}), ())
    # This case proves connected JSON-plan reconstruction. Missing commands
    # must still refuse before any attempt, resource provisioning or model work.
    with pytest.raises(AccessDenied, match="Installed worker verification sources are required"):
        CodeStageSource(author, service, inspection)
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
