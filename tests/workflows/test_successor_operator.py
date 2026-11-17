"""Authored provider authority with real stores, stopped owner and literal CLI."""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.workflow_control.identity import (
    endpoint_binding,
    file_binding,
    process_start,
    source_digest,
)
from ai_task_manager.adapters.workflow_control.recovery import RecoveryHistory, physical
from ai_task_manager.application.service.linear import DECISION_KINDS
from ai_task_manager.application.workflows.models import WorkflowPlan
from ai_task_manager.config.claim_store import enroll_claim_store
from ai_task_manager.config.files import digest, read_private, write_private
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.installation.sessions.successor import setup
from ai_task_manager.installation.workflows.bootstrap import activation_reference
from ai_task_manager.installation.workflows.successor import prepare
from ai_task_manager.storage import work
from ai_task_manager.storage.workflows import WorkflowStore
from ai_task_manager.supervisor.journal import RuntimeJournal
from tests.domains.helpers import Rig


@pytest.fixture
def staged(tmp_path, request):
    root = tmp_path.resolve()
    root.chmod(0o700)
    r = Rig(root / "core.sqlite")
    r.now = time.time()
    Path(r.db.path).chmod(0o600)
    task = r.queued(code=True, units=("first", "remaining"))
    with r.db.transaction() as conn:
        task = work.save(conn, replace(task, completed_work=("first",)), task.version)
    journal = AuthorityJournal(root / "authority.sqlite", root / "authority.key")
    enroll_claim_store(r.db, journal)
    runtime = RuntimeJournal(root / "runtime.sqlite")
    host = HostJournal(root / "host.sqlite", root / "host.key")
    provider = ProviderJournal(root / "provider.sqlite")
    for name in ("runtime.key", "review.key"):
        (root / name).write_bytes(secrets.token_bytes(32))
        (root / name).chmod(0o600)
    SessionFile(root / "session.json").save(r.handle, r.scope)
    expected = {
        "viewer_id": r.scope.principal,
        "workspace_id": r.scope.workspace,
        "team_id": "team",
        "team_key": "TEAM",
        "project_id": r.scope.project,
    }
    config = {
        "version": 1,
        "core_database": str(r.db.path),
        "authority_database": str(journal.path),
        "authority_key": str(journal.key_path),
        "session_file": str(root / "session.json"),
        "approval_policy": asdict(r.policy),
        "linear": {"journal": str(provider.path), "expected": expected},
    }
    # Socket length is independent of pytest's temporary path depth.
    import tempfile

    sockets = Path(tempfile.mkdtemp(prefix="suc-", dir="/tmp")).resolve()
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import sys;sys.stdin.read()"], stdin=subprocess.PIPE
    )

    def stop_child():
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)

    request.addfinalizer(stop_child)
    stream = socket.socket(socket.AF_UNIX)
    endpoint = sockets / "owner.sock"
    stream.bind(str(endpoint))
    endpoint.chmod(0o600)
    config["workflow_control"] = {
        "record": str(sockets / "owner.json"),
        "endpoint": str(endpoint),
        "instance": str(uuid4()),
    }
    original = root / "service.json"
    write_private(original, config)
    descriptor = {
        "version": 1,
        "instance": config["workflow_control"]["instance"],
        "scope": asdict(r.scope),
        "config": file_binding(original),
        "source": source_digest(),
        "pid": child.pid,
        "start": process_start(child.pid),
        "endpoint": endpoint_binding(endpoint),
        "key": "a" * 64,
    }
    write_private(config["workflow_control"]["record"], descriptor)
    store = WorkflowStore(root / "workflow.sqlite")
    old_plan = WorkflowPlan(
        "old", "assignment", "claude", "b" * 40, ("result",), ("result",), r.now - 1
    )
    history = RecoveryHistory(
        original,
        core=r.db.path,
        workflow=store.path,
        runtime=runtime.path,
        host=host.path,
        provider=provider.path,
    ).capture(r.scope)
    ref = store.create(r.scope, task, old_plan, "original-key", "old-runtime", history)
    base_spec = {
        "version": 1,
        "config": file_binding(original),
        "scope": asdict(r.scope),
        "task_id": task.id,
        "prose_digest": task.prose_digest,
        "native_ref": "authored-native",
        "native_digest": "a" * 64,
        "repository": str(root),
        "source_head": "b" * 40,
        "state_root": str(root),
        "deadline": old_plan.deadline,
        "principal_ref": "principal",
        "plan_ref": old_plan.id,
        "files": ["result"],
        "artifacts": ["result"],
        "installation_path": str(root / "installation.json"),
    }
    write_private(root / "installation.json", base_spec)
    files = {
        name: physical(path)
        for name, path in {
            "core": r.db.path,
            "workflow": store.path,
            "runtime": runtime.path,
            "host": host.path,
            "provider": provider.path,
        }.items()
    }
    for name, path in {
        "runtime_key": root / "runtime.key",
        "host_key": root / "host.key",
        "review_key": root / "review.key",
        "session": root / "session.json",
        "authority": journal.path,
        "authority_key": journal.key_path,
        "dependency": root / "dependency.sqlite",
        "dependency_key": root / "dependency.key",
    }.items():
        if not Path(path).exists():
            Path(path).write_bytes(secrets.token_bytes(32))
            Path(path).chmod(0o600)
        files[name] = (
            file_binding(path) if name.endswith("key") or name == "session" else physical(path)
        )
    activation = {
        "config": file_binding(original),
        "scope": asdict(r.scope),
        "instance": descriptor["instance"],
        "source": source_digest(),
        "deadline": old_plan.deadline,
        "recovery": {
            "installation": file_binding(root / "installation.json"),
            "workflow": {**physical(store.path), "store_id": store.store_id},
            "files": files,
            "owner": {"pid": child.pid, "start": descriptor["start"]},
        },
    }
    bind_record(journal, activation_reference(original, config["workflow_control"]), activation)
    child.stdin.close()
    child.wait(timeout=5)
    stream.close()
    endpoint.unlink()
    fresh = root / "successor.json"
    fresh_data = {
        **config,
        "session_file": str(root / "successor-session.json"),
        "workflow_control": {
            "record": str(sockets / "new.json"),
            "endpoint": str(sockets / "new.sock"),
            "instance": str(uuid4()),
        },
    }
    write_private(fresh, fresh_data)
    from ai_task_manager.adapters.linear.models import ExpectedBinding

    binding = SimpleNamespace(expected=ExpectedBinding(**expected))
    client = SimpleNamespace(verify_binding=lambda: binding, check_connection=lambda *a, **k: None)
    components = SimpleNamespace(
        configuration_hash=digest(config["linear"]),
        client=client,
        evidence=lambda authority: EvidenceRouter(
            [EvidenceRoute("decision-proof:", DECISION_KINDS, authority)]
        ),
    )
    setup(
        fresh,
        original,
        actions=("successor", "start"),
        deadline=time.time() + 180,
        operation_key="fresh-enrollment",
        components=components,
    )
    spec = {
        **base_spec,
        "config": file_binding(fresh),
        "deadline": time.time() + 150,
        "plan_ref": "fresh",
        "installation_path": str(root / "successor-installation.json"),
    }
    write_private(root / "successor-installation.json", spec)
    r.core.revoke_session(r.handle)
    intent = prepare(
        original,
        root / "installation.json",
        fresh,
        root / "successor-installation.json",
        workflow_ref=ref,
        original_key="original-key",
        operation_key="successor-key",
        components=components,
    )
    try:
        yield root, r, store, ref, intent
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        import shutil

        shutil.rmtree(sockets)


def test_literal_successor_admission_preserves_original_key_and_revocation(staged):
    root, r, store, ref, intent = staged
    before = store.read(ref, r.scope)
    installed = os.environ.get("AITM_SUCCESSOR_PYTHON")
    command = (
        [str(Path(installed).with_name("aitm"))]
        if installed
        else [sys.executable, "-m", "ai_task_manager"]
    )
    command += [
        "--config",
        str(root / "service.json"),
        "--json",
        "--installation",
        str(root / "installation.json"),
        "workflow",
        "recover",
        "--input",
        "-",
    ]
    request = {
        "version": 2,
        "action": "successor",
        "workflow_ref": ref,
        "operation_key": "original-key",
        "authorization_ref": intent,
    }
    env = dict(os.environ)
    if os.environ.get("AITM_SUCCESSOR_PYTHON"):
        env.pop("PYTHONPATH", None)
    for _ in range(2):
        result = subprocess.run(
            command, input=json.dumps(request), text=True, capture_output=True, timeout=20, env=env
        )
        assert result.returncode == 0, result.stdout + result.stderr
        response = json.loads(result.stdout)
        assert response["data"]["state"] == "successor_admitted"
        successor = response["data"]["workflow_ref"]
    assert store.read(ref, r.scope) == before
    assert store.find(r.scope, before["task_id"], "original-key") == ref
    assert store.find(r.scope, before["task_id"], "successor-key") == successor
    fresh_handle = read_private(root / "successor-session.json")["capability"]
    from ai_task_manager.domains.work.models import SessionHandle

    assert r.core.get_task(SessionHandle(fresh_handle), before["task_id"]).completed_work == (
        "first",
    )
    with r.db.transaction() as conn:
        assert conn.execute("SELECT revoked FROM sessions").fetchone()[0] == 1
    report = os.environ.get("AITM_SUCCESSOR_REPORT")
    if report:
        Path(report).write_text(
            json.dumps(
                {
                    "root": str(root),
                    "command": command,
                    "request": request,
                    "response": response,
                    "source": source_digest(),
                    "evidence_kind": "authored provider/authority, real local owner/store/CLI",
                },
                indent=2,
            )
        )


def test_successor_loader_does_not_initialize_missing_retained_history(tmp_path):
    from ai_task_manager.config.service import load_service
    from ai_task_manager.domains.work.models import Scope, SessionHandle

    root = tmp_path.resolve()
    root.chmod(0o700)
    SessionFile(root / "session.json").save_successor(
        SessionHandle("x" * 32), Scope("linear", "workspace", "principal", "project", "logical")
    )
    config = {
        "version": 1,
        "core_database": str(root / "missing-core.sqlite"),
        "authority_database": str(root / "missing-authority.sqlite"),
        "authority_key": str(root / "missing-authority.key"),
        "session_file": str(root / "session.json"),
        "approval_policy": {
            "writer_hash": "a" * 64,
            "reference_hash": "b" * 64,
            "style_hash": "c" * 64,
        },
    }
    write_private(root / "service.json", config)
    with pytest.raises(
        (
            OSError,
            __import__(
                "ai_task_manager.domains.work.errors", fromlist=["AccessDenied"]
            ).AccessDenied,
        )
    ):
        load_service(root / "service.json")
    assert not any(
        (root / name).exists()
        for name in ("missing-core.sqlite", "missing-authority.sqlite", "missing-authority.key")
    )
