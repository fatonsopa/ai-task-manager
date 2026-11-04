"""One installed cycle; declared HTTP/native contracts, no real provider/model effects."""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.installation.native import NativeManifest, normal
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.store import NativeStore
from ai_task_manager.presentation.cli.main import main
from tests.application.helpers import FEATURE_BODY, reviewed_control
from tests.complete_cycle.continuation import finish_tail, prepare_tail
from tests.complete_cycle.provider import CycleProvider
from tests.module_core.fixture import Demonstration
from tests.native_installation.helpers import bundle, refresh_digest
from tests.native_installation.test_lead_startup import DeclaredRestrictions


def test_complete_installed_scripted_cycle(tmp_path, monkeypatch):
    out = Path(os.environ["COMPLETE_CYCLE_EVIDENCE"])
    root = tmp_path / "scenario"
    root.mkdir(mode=0o700)
    db = tmp_path / "core.sqlite"
    db.touch(mode=0o600)
    sockets = tempfile.TemporaryDirectory(prefix="cc-", dir="/tmp")
    fixture = None
    thread = None
    stop = threading.Event()
    ready = threading.Event()
    records = []
    owners = []
    all_native = []
    failures = []
    import importlib
    import traceback

    cli_module = importlib.import_module("ai_task_manager.presentation.cli.main")
    original_failure = cli_module.failure

    def observe_failure(error):
        (out / "classified-failure.txt").write_text(traceback.format_exc())
        return original_failure(error)

    monkeypatch.setattr(cli_module, "failure", observe_failure)
    facade_module = importlib.import_module("ai_task_manager.application.service.facade")
    monkeypatch.setattr(facade_module, "failure", observe_failure)
    try:
        from uuid import uuid4

        fixture = Demonstration(
            root,
            db,
            Path("/Users/faton/Documents/ChatGPT/technical-writer"),
            provider_factory=CycleProvider,
            capacity_policies=[{"host": "claude", "host_limit": 2, "project_limit": 2}],
            workflow_control={
                "record": str(Path(sockets.name).resolve() / "owner.json"),
                "endpoint": str(Path(sockets.name).resolve() / "owner.sock"),
                "instance": str(uuid4()),
            },
        )
        monkeypatch.setattr(HttpTransport, "ENDPOINT", fixture.endpoint)

        def call(group, action, data=None, extra=()):
            operation = group + "_" + action
            if not ready.is_set():
                data = {"operation": operation, "data": data or {}}
                group, action = "workflow", "prepare"
            args = ["--config", str(fixture.config), "--json", *extra, group, action]
            if data is not None:
                p = root / "request.json"
                p.write_text(json.dumps(data))
                p.chmod(0o600)
                args += ["--input", str(p)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(args)
            row = {
                "group": group,
                "action": action,
                "input": data,
                "code": code,
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
            }
            records.append(row)
            (out / "commands.json").write_text(json.dumps(records, indent=2) + "\n")
            value = json.loads(row["stdout"] or row["stderr"])
            assert code == 0, row
            return value["data"]

        prepared = call(
            "writing",
            "prepare",
            {
                "kind": "issue_feature",
                "purpose": "task",
                "title": "Produce the owned non-code report",
                "body": FEATURE_BODY,
                "code_bearing": False,
                "required_work": ["complete"],
            },
        )
        call(
            "writing",
            "review",
            {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
        )
        approved = call("writing", "finalize", {"writing_id": prepared["writing_id"]})
        task = call(
            "task",
            "submit",
            {"writing_ref": approved["approval_ref"], "operation_key": "cycle-submit"},
        )
        assert task["observed_status"] == "Backlog"
        task = call(
            "task",
            "action",
            {
                "task_id": task["id"],
                "action": "enqueue",
                "expected_version": task["version"],
                "decision_revision": task["decision_revision"],
                "operation_key": "cycle-queue",
            },
        )
        assert task["observed_status"] == "Todo" and task["sync_state"] == "confirmed"
        repo = root / "repository"
        repo.mkdir()
        for cmd in [
            ["git", "init", "-q", str(repo)],
            ["git", "-C", str(repo), "config", "user.name", "Declared cycle"],
            ["git", "-C", str(repo), "config", "user.email", "cycle@example.invalid"],
        ]:
            subprocess.run(cmd, check=True, capture_output=True)
        (repo / "README.md").write_text("Owned non-code cycle fixture.\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "Initial owned fixture"], check=True
        )
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        deadline = time.time() + 120
        tail = prepare_tail(
            config=fixture.config,
            call=call,
            root=root,
            repository=repo,
            source_head=head,
            author_task=task,
            deadline=deadline,
            monkeypatch=monkeypatch,
        )
        b = bundle(root / "worker", "claude", "claude-opus-5-5", task_id=task["id"])
        executable = b.root / "declared-native"
        executable.write_text("#!" + sys.executable + "\n" + tail["author_script"])
        executable.chmod(0o700)
        store = NativeStore.create(b.root / "normal.sqlite")
        service = load_service(fixture.config)
        spec = {
            "version": 1,
            "config": file_binding(fixture.config),
            "scope": asdict(service.scope),
            "task_id": task["id"],
            "prose_digest": task["prose_digest"],
            "native_ref": "normal-native",
            "native_digest": "0" * 64,
            "native_manifest": str(b.manifest),
            "repository": str(repo),
            "source_head": head,
            "state_root": str(b.root / "state"),
            "deadline": deadline,
            "reviewer_installation": tail["reviewer_installation"],
            "principal_ref": "declared-worker",
            "plan_ref": "complete-cycle",
            "files": ["report.txt"],
            "artifacts": ["report.txt"],
            "installation_path": str(b.installation),
        }
        write_private(b.installation, spec, replace=True)
        b.data.update(
            authority_store=store.pointer,
            config=file_binding(fixture.config),
            installation=installation_intent(b.installation),
            executable=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            account_hash=claude_account_hash(
                {
                    "email": "fixture@example.invalid",
                    "orgId": "authored-org",
                    "userId": "authored-user",
                }
            ),
        )
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)
        manifest = NativeManifest(b.manifest, fixture.config, b.installation)
        normal.enroll(
            store,
            manifest,
            {"host": 60, "relay": 60, "auth": 5, "neutral": 8, "assignment": 8},
            identity_refreshes=(),
            conformance_ref="declared-contract",
        )
        original_source = normal.NormalAuthoritySource
        monkeypatch.setattr(
            normal,
            "NormalAuthoritySource",
            lambda *a, **kw: original_source(*a, **{**kw, "conformance": DeclaredRestrictions()}),
        )
        import ai_task_manager.installation.workflows as api
        from ai_task_manager.installation.workflows.composition import InstalledWorkflow

        original_launch = api.launch
        original_init = InstalledWorkflow.__init__

        def observe_init(self, *a, **kw):
            original_init(self, *a, **kw)
            owners.append(self)

        monkeypatch.setattr(InstalledWorkflow, "__init__", observe_init)
        monkeypatch.setattr(
            api,
            "launch",
            lambda *a, **kw: original_launch(*a, **{**kw, "stop": stop, "ready": ready.set}),
        )

        def own():
            try:
                code = main(
                    [
                        "--config",
                        str(fixture.config),
                        "--installation",
                        str(b.installation),
                        "--json",
                        "workflow",
                        "owner",
                    ]
                )
                assert code == 0
            except BaseException:
                import traceback

                failures.append(traceback.format_exc())

        thread = threading.Thread(target=own)
        thread.start()
        assert ready.wait(8), failures
        result = call(
            "workflow",
            "run",
            {"task_id": task["id"], "plan_ref": "complete-cycle", "operation_key": "cycle-run"},
        )
        assert result["state"] == "done", result
        completed = finish_tail(
            config=fixture.config,
            call=call,
            root=root,
            author_task=task,
            workflow_result=result,
            monkeypatch=monkeypatch,
        )
        (out / "complete-cycle-result.json").write_text(json.dumps(completed, indent=2) + "\n")
    finally:
        stop.set()
        if thread is not None:
            thread.join(12)
        assert thread is None or not thread.is_alive()
        for owner in owners:
            all_native.extend(owner.factory.owned.values())
            factory = owner.registry.reviewer_factory
            if factory is not None:
                for producer in factory.producers.values():
                    all_native.extend(producer.workers.hosts.owned.values())
        for native in all_native:
            if native.process is not None and not native.process._closed:
                assert native.stop().confirmed
        if fixture is not None:
            fixture.close()
        sockets.cleanup()
        (out / "settlement.json").write_text(
            json.dumps(
                {
                    "owner_thread_stopped": thread is None or not thread.is_alive(),
                    "owner_failures": failures,
                    "provider_stopped": fixture is None or not fixture.provider.thread.is_alive(),
                    "owned_native": len(all_native),
                    "all_native_closed": all(
                        n.process is None or n.process._closed
                        for n in all_native
                    ),
                    "commands": len(records),
                },
                indent=2,
            )
            + "\n"
        )
