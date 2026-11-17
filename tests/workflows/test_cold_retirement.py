"""Installed retirement after a real owned process stop, with authored host ports."""

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.parametrize("cold_owner", ["cold-active"], indirect=True)
def test_installed_retirement_lost_reply_and_original_key_replay(cold_owner):
    root, owner, call = cold_owner
    ready = json.loads((root / "prepared.json").read_text())
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            call,
            "run",
            {
                "task_id": ready["task_id"],
                "plan_ref": ready["plan_ref"],
                "operation_key": "retirement-original",
            },
            installed=False,
        )
        end = time.monotonic() + 12
        while not (root / "native-entered").exists() and time.monotonic() < end:
            time.sleep(0.02)
        assert (root / "native-entered").exists()
        owner.kill()
        owner.wait(timeout=5)
        _, initial = running.result(timeout=15)
    # The public run may lose its reply after the owner's death. Resolve only
    # its original durable key, without reconstructing a workflow.
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.workflow_control.lineage import resolve_control
    from ai_task_manager.config.files import read_private
    from ai_task_manager.domains.work.models import Scope
    from ai_task_manager.installation.sessions.recovery import history
    from ai_task_manager.installation.workflows.bootstrap import activation_reference
    from ai_task_manager.storage.workflows import WorkflowStore

    config = read_private(root / "service.json")
    journal = AuthorityJournal.open_existing(config["authority_database"], config["authority_key"])
    activated = history(
        journal, activation_reference(root / "service.json", resolve_control(root / "service.json"))
    )
    binding = activated["recovery"]["workflow"]
    store = WorkflowStore.open_existing(binding["path"], store_id=binding["store_id"])
    scope = (
        Scope(**ready["scope"])
        if "scope" in ready
        else Scope(**read_private(root / "session.json")["scope"])
    )
    ref = store.find(scope, ready["task_id"], "retirement-original")
    request = {"workflow_ref": ref, "operation_key": "retirement-original", "action": "stop"}
    stopped, result = call("recover", request)
    assert stopped.returncode == 0, result
    from ai_task_manager.application.core import Core
    from ai_task_manager.config.claim_store import open_claim_store
    from ai_task_manager.domains.work.errors import AccessDenied
    from ai_task_manager.domains.work.models import ApprovalPolicy, SessionHandle

    database, _ = open_claim_store(config["core_database"], journal, existing_only=True)
    core = Core(database, ApprovalPolicy(**config["approval_policy"]))
    old_handle = SessionHandle(read_private(root / "session.json")["capability"])
    core.revoke_session(old_handle)
    with pytest.raises(AccessDenied):
        core.execution.reserve(
            old_handle, ready["task_id"], "fixture", 0, 0, "unavailable", "denied"
        )
    request["action"] = "retire"
    import os
    from pathlib import Path

    cli = Path(os.environ["AITM_OPERATOR_TEST_VENV"]) / "bin/aitm"
    child = subprocess.Popen(
        [
            str(cli),
            "--config",
            str(root / "service.json"),
            "--json",
            "--installation",
            str(root / "installation.json"),
            "workflow",
            "recover",
            "--input",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=call.environment,
    )
    child.stdout.close()
    child.stdin.write(json.dumps(request).encode())
    child.stdin.close()
    child.wait(timeout=25)
    assert child.returncode != 0
    replayed, result = call("recover", request)
    assert replayed.returncode == 0 and result["data"]["state"] == "retired", result
    assert result["data"]["resources"] == {"worktree": "removed", "database": "removed"}
    with pytest.raises(AccessDenied):
        core.session_binding(old_handle)
