"""Real killed/replacement owners through a stock installed CLI with authored ports."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def cold_owner(tmp_path, request):
    root = tmp_path.resolve()
    root.chmod(0o700)
    sockets = tempfile.TemporaryDirectory(prefix="dc-", dir=str(Path("/tmp").resolve()))
    package = Path(os.environ["WORKFLOW_INSTALL_PACKAGE"])
    repo = Path(__file__).parents[2]
    control = root / "control"
    control.mkdir(mode=0o700)
    (control / "sitecustomize.py").write_text(
        "from tests.workflows.cold_controls import install;install()\n"
    )
    env = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            map(str, (control, package, repo, Path(pytest.__file__).parent.parent))
        ),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    output = (root / "owner.log").open("wb")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tests.workflow_installation.helpers import owner_main;"
            "import sys;owner_main(*sys.argv[1:])",
            str(root),
            sockets.name,
            getattr(request, "param", "cold"),
        ],
        stdout=output,
        stderr=output,
        env=env,
        start_new_session=True,
    )
    end = time.monotonic() + 12
    while not (root / "ready.json").exists() and child.poll() is None and time.monotonic() < end:
        time.sleep(0.02)
    assert (root / "ready.json").exists(), (root / "owner.log").read_text()
    cli = Path(os.environ["AITM_OPERATOR_TEST_VENV"]) / "bin/aitm"

    def call(action, value, *, installed=True):
        command = [str(cli), "--config", str(root / "service.json"), "--json"]
        if installed:
            command += ["--installation", str(root / "installation.json")]
        command += ["workflow", action, "--input", "-"]
        observed = subprocess.run(
            command, input=json.dumps(value), text=True, capture_output=True, timeout=25, env=env
        )
        return observed, json.loads(observed.stdout or observed.stderr)

    call.environment = env

    try:
        yield root, child, call
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=8)
        # Replacement owners retain their original finite deadline. Ask them to
        # close through their actual owned test handles where available below.
        output.close()
        sockets.cleanup()


def test_cold_original_config_resume_uses_a_different_owner(cold_owner):
    root, child, call = cold_owner
    ready = json.loads((root / "prepared.json").read_text())
    first, result = call(
        "run",
        {
            "task_id": ready["task_id"],
            "plan_ref": ready["plan_ref"],
            "operation_key": "cold-original",
        },
        installed=False,
    )
    assert first.returncode == 4 and result["data"]["state"] == "awaiting_review", result
    config = (root / "service.json").read_bytes()
    spec = (root / "installation.json").read_bytes()
    from ai_task_manager.adapters.workflow_control.lineage import resolve_control

    original_control = resolve_control(root / "service.json")
    original_record = Path(original_control["record"]).read_bytes()
    child.kill()
    child.wait(timeout=5)
    request = {
        "action": "resume",
        "workflow_ref": result["data"]["workflow"],
        "operation_key": "cold-original",
    }
    resumed, response = call("recover", request)
    assert resumed.returncode == 4 and response["data"] == result["data"], response
    successor = resolve_control(root / "service.json")
    descriptor = json.loads(Path(successor["record"]).read_text())
    assert descriptor["pid"] != child.pid
    assert successor["instance"] != original_control["instance"]
    assert Path(original_control["record"]).read_bytes() == original_record
    assert (root / "service.json").read_bytes() == config
    assert (root / "installation.json").read_bytes() == spec
    # Never signal a descriptor PID: the replacement is allowed to finish its
    # original finite lifetime, and the test waits for its exact endpoint exit.
    end = time.monotonic() + 42
    while Path(successor["endpoint"]).exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert not Path(successor["endpoint"]).exists()


@pytest.mark.parametrize("cold_owner", ["cold-active"], indirect=True)
@pytest.mark.parametrize("change", [None, "hold", "cancel", "reassignment", "revoked"])
def test_cold_stop_waits_for_native_and_stops_original_harness(cold_owner, change):
    from concurrent.futures import ThreadPoolExecutor

    from ai_task_manager.adapters.process.recovery import observe_recorded_group
    from ai_task_manager.domains.work.models import Scope
    from ai_task_manager.storage.workflows import WorkflowStore
    from ai_task_manager.supervisor.journal import RuntimeJournal

    root, child, call = cold_owner
    ready = json.loads((root / "prepared.json").read_text())
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(
            call,
            "run",
            {
                "task_id": ready["task_id"],
                "plan_ref": ready["plan_ref"],
                "operation_key": "cold-active",
            },
            installed=False,
        )
        end = time.monotonic() + 10
        while not (root / "native-entered").exists() and time.monotonic() < end:
            time.sleep(0.02)
        assert (root / "native-entered").exists()
        child.kill()
        child.wait(timeout=5)
        running.result(timeout=5)
    import sqlite3

    path = root / "installed-state/workflow.sqlite"
    with sqlite3.connect(path) as db:
        store_id = db.execute("SELECT store_id FROM workflow_meta").fetchone()[0]
    scope = Scope(**json.loads((root / "session.json").read_text())["scope"])
    store = WorkflowStore.open_existing(path, store_id=store_id)
    ref = store.find(scope, ready["task_id"], "cold-active")
    row = store.read(ref, scope)
    journal = RuntimeJournal.open_existing(root / "installed-state/runtime.sqlite")
    process = journal.observations(row["attempt_id"], "process_created")[0]
    before = observe_recorded_group(process["pid"], process["start_identity"], process["group"])
    assert before.known and before.live_members
    human = None
    if change in {"hold", "cancel", "reassignment"}:
        from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
        from ai_task_manager.config.service import load_service

        service = load_service(
            root / "service.json",
            existing_only=True,
            workflow_factory=lambda *_: None,
            evidence_factory=lambda j: EvidenceRouter(
                [EvidenceRoute("decision-proof:", frozenset({"external_observation"}), j)]
            ),
        )
        task = service.core.get_task(service.handle, ready["task_id"])
        proof = service.decisions.journal._evidence(
            "external_observation",
            scope,
            task.id,
            {
                "issue_id": task.issue_id,
                "source_revision": "9999999999",
                "status": "Canceled"
                if change == "cancel"
                else "On Hold"
                if change == "hold"
                else "In Progress",
                "title": task.title,
                "body": task.body,
                "assigned_to_owner": change != "reassignment",
                "actor_kind": "human",
                "actor_authorized": True,
            },
            "authored-current-human-observation",
        )
        service.core.external.observe(
            service.handle,
            task.id,
            task.version,
            task.decision_revision,
            proof,
            "current-human-change",
        )
        human = service.core.get_task(service.handle, task.id)
        assert human.human_hold is True
    elif change == "revoked":
        call.environment["COLD_TEST_GRANT_STATE"] = "revoked"
    stopped, response = call(
        "recover", {"action": "stop", "workflow_ref": ref, "operation_key": "cold-active"}
    )
    assert stopped.returncode == 0 and response["data"]["quiescent"] is True, response
    assert response["data"]["state"] == "paused"
    assert all(response["data"]["components"].values())
    after = observe_recorded_group(process["pid"], process["start_identity"], process["group"])
    assert after.known and not after.live_members
    with sqlite3.connect(root / "core.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert db.execute("SELECT active FROM attempts").fetchone()[0] == 0
    assert store.read(ref, scope)["steps"] == row["steps"]
    if human is not None:
        current = service.core.get_task(service.handle, ready["task_id"])
        assert current.human_hold and current.decision_ref == human.decision_ref
        assert current.decision_revision == human.decision_revision
    resumed, failure = call(
        "recover", {"action": "resume", "workflow_ref": ref, "operation_key": "cold-active"}
    )
    assert resumed.returncode == 3 and failure["ok"] is False


@pytest.mark.parametrize("cold_owner", ["cold-active"], indirect=True)
@pytest.mark.parametrize("change", ["missing_runtime", "foreign_socket", "unknown", "native_fact"])
def test_cold_missing_foreign_and_unknown_keep_capacity_and_history(cold_owner, change):
    import socket
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    root, child, call = cold_owner
    ready = json.loads((root / "prepared.json").read_text())
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(
            call,
            "run",
            {
                "task_id": ready["task_id"],
                "plan_ref": ready["plan_ref"],
                "operation_key": "protected",
            },
            installed=False,
        )
        end = time.monotonic() + 10
        while not (root / "native-entered").exists() and time.monotonic() < end:
            time.sleep(0.02)
        assert (root / "native-entered").exists()
        child.kill()
        child.wait(timeout=5)
        active.result(timeout=5)
    with sqlite3.connect(root / "installed-state/workflow.sqlite") as db:
        ref = db.execute("SELECT id FROM runs WHERE operation_key='protected'").fetchone()[0]
    request = {"action": "stop", "workflow_ref": ref, "operation_key": "protected"}
    runtime = root / "installed-state/runtime.sqlite"
    control = json.loads((root / "service.json").read_text())["workflow_control"]
    endpoint = Path(control["endpoint"])
    foreign = None
    native = None
    if change == "missing_runtime":
        runtime.rename(runtime.with_suffix(".retained"))
    elif change == "foreign_socket":
        endpoint.rename(endpoint.with_suffix(".retained"))
        foreign = socket.socket(socket.AF_UNIX)
        foreign.bind(str(endpoint))
        endpoint.chmod(0o600)
    elif change == "unknown":
        call.environment["COLD_TEST_UNKNOWN_INVENTORY"] = "1"
    else:
        with sqlite3.connect(root / "installed-state/host.sqlite") as db:
            native = next(
                row
                for row in db.execute("SELECT ref,data,mac FROM host_records")
                if json.loads(row[1])["payload"].get("effect") == "native_process_created"
            )
            db.execute("DELETE FROM host_records WHERE ref=?", (native[0],))
    try:
        refused, failure = call("recover", request)
        assert refused.returncode in {3, 6} and failure["ok"] is False, failure
        with sqlite3.connect(root / "core.sqlite") as db:
            assert db.execute("SELECT active FROM attempts").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 2
        if change == "missing_runtime":
            assert not runtime.exists()
        if foreign is not None:
            assert foreign.fileno() >= 0 and endpoint.is_socket()
    finally:
        if change == "missing_runtime":
            runtime.with_suffix(".retained").rename(runtime)
        elif change == "foreign_socket":
            foreign.close()
            endpoint.unlink()
            endpoint.with_suffix(".retained").rename(endpoint)
        elif change == "unknown":
            call.environment.pop("COLD_TEST_UNKNOWN_INVENTORY")
        else:
            with sqlite3.connect(root / "installed-state/host.sqlite") as db:
                db.execute("INSERT INTO host_records VALUES(?,?,?)", native)
    stopped, response = call("recover", request)
    assert stopped.returncode == 0 and response["data"]["quiescent"] is True, response
