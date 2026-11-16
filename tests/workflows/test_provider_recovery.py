"""T03 literal installed recovery over authored HTTP, with no native launch."""

import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.claim_store import enroll_claim_store
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.models import ApprovalPolicy, CapacityPolicy
from ai_task_manager.installation.sessions import setup
from tests.linear.helpers import CONNECTION, EXPECTED, LinearRig, ProviderFixture
from tests.workflows.provider_recovery_fixture import registry


def test_t03_installed_original_key_readback_after_owner_and_grant_expiry(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    provider = ProviderFixture()
    owner = None
    sockets = tempfile.TemporaryDirectory(prefix="t03-", dir=str(Path("/tmp").resolve()))
    try:
        rig = LinearRig(root, provider)
        rig.now = time.time()
        rig.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
        rig.capacity = CapacityPolicy("claude", 2, 2)
        rig.core = rig.new_core()
        rig.scope = replace(rig.scope, logical_session="t03-provider-session")
        connection = replace(CONNECTION, logical_session=rig.scope.logical_session)
        Path(rig.db.path).chmod(0o600)
        authority = AuthorityJournal(root / "authority.sqlite", root / "authority.key")
        enroll_claim_store(rig.db, authority)
        credentials = root / "credentials.env"
        credentials.write_text("LINEAR_TEST_USER_A_TOKEN=fixture-api-secret\n")
        credentials.chmod(0o600)
        config = root / "service.json"
        deadline = time.time() + 9
        write_private(
            config,
            {
                "version": 1,
                "core_database": str(rig.db.path),
                "authority_database": str(authority.path),
                "authority_key": str(authority.key_path),
                "session_file": str(root / "session.json"),
                "approval_policy": asdict(rig.policy),
                "capacity_policies": [asdict(rig.capacity)],
                "workflow_control": {
                    "record": sockets.name + "/owner.json",
                    "endpoint": sockets.name + "/owner.sock",
                    "instance": "969ba294-a087-42db-980d-532a8a07632b",
                },
                "linear": {
                    "expected": asdict(EXPECTED),
                    "agent_connection": asdict(connection),
                    "credential_file": str(credentials),
                    "credential_key": "LINEAR_TEST_USER_A_TOKEN",
                    "journal": str(rig.journal.path),
                },
            },
        )
        from ai_task_manager.adapters.linear.client import LinearClient
        from ai_task_manager.adapters.linear.transport import HttpTransport
        from ai_task_manager.application.service.linear import LinearComponents

        components = LinearComponents(
            read_private(config)["linear"],
            client_factory=lambda cred, expected: LinearClient(
                HttpTransport(_test_endpoint=provider.endpoint), cred, expected
            ),
        )
        setup(
            config,
            logical_session=rig.scope.logical_session,
            actions=("start", "enqueue"),
            deadline=deadline,
            operation_key="t03-enrollment",
            components=components,
        )
        rig.handle = SessionFile(root / "session.json").load(rig.core)
        rig.client = components.client
        from ai_task_manager.application.linear_sync.outbound import OutboundDrain

        rig.outbound = OutboundDrain(rig.core, rig.client, rig.journal)
        task = rig.queued(code=False)
        project = root / "project"
        project.mkdir(mode=0o700)
        environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        for args in [
            ["init", "-q", str(project)],
            ["-C", str(project), "config", "user.email", "fixture@example.invalid"],
            ["-C", str(project), "config", "user.name", "Authored fixture"],
        ]:
            subprocess.run(
                ["git", *args], env=environment, check=True, capture_output=True, timeout=5
            )
        (project / "protected.txt").write_text("Retain the original fixture input.\n")
        subprocess.run(
            ["git", "-C", str(project), "add", "."], check=True, capture_output=True, timeout=5
        )
        subprocess.run(
            ["git", "-C", str(project), "commit", "-qm", "Prepare authored input"],
            env=environment,
            check=True,
            capture_output=True,
            timeout=5,
        )
        source = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "HEAD"], text=True, timeout=5
        ).strip()
        native = registry().require("authored-no-native")
        installation = root / "installation.json"
        write_private(
            installation,
            {
                "version": 1,
                "installation_path": str(installation),
                "config": file_binding(config),
                "scope": asdict(rig.scope),
                "task_id": task.id,
                "prose_digest": task.prose_digest,
                "native_ref": native.reference,
                "native_digest": native.fingerprint,
                "repository": str(project),
                "source_head": source,
                "state_root": str(root / "state"),
                "deadline": deadline,
                "principal_ref": "authored-unused-principal",
                "plan_ref": "t03-provider",
                "files": ["protected.txt"],
                "artifacts": ["protected.txt"],
            },
        )
        base = Path(os.environ["AITM_OPERATOR_TEST_VENV"])
        bootstrap = Path(__file__).parents[1] / "receiver/installed_bootstrap.py"
        owner_entry = root / "owner.py"
        owner_entry.write_text(
            "import sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];"
            "from tests.workflows.provider_recovery_fixture import owner_main;"
            "owner_main(*sys.argv[1:])"
        )
        # The existing test bootstrap redirects only the HTTP transport. Product
        # CLI/recovery and all durable identity/claim checks remain installed.
        args = [
            str(base / "bin/python"),
            "-I",
            str(bootstrap),
            provider.endpoint,
            str(owner_entry),
            str(Path(__file__).parents[2]),
            str(Path(pytest.__file__).parent.parent),
            str(config),
            str(installation),
            str(root / "ready.json"),
        ]
        with (root / "owner.log").open("wb") as output:
            owner = subprocess.Popen(
                args,
                stdout=output,
                stderr=output,
                env={k: v for k, v in environment.items() if k != "PYTHONPATH"},
            )
            until = time.time() + 6
            while (
                not (root / "ready.json").exists() and owner.poll() is None and time.time() < until
            ):
                time.sleep(0.02)
            assert (root / "ready.json").exists(), (root / "owner.log").read_text()
            reference = read_private(root / "ready.json")["workflow_ref"]
            # Authored Core startup records stage a legitimate pending status;
            # no physical/native startup or quiescence is claimed by this case.
            task, attempt = rig.starting(task)

            def lost(request, result):
                if "mutation UpdateIssue" in request["query"]:
                    provider.fail_next = (503, b"lost readback")

            provider.hook = lost
            first = rig.outbound.run_once(rig.handle)
            provider.hook = None
            assert first["state"] == "uncertain"
            sends = len([r for r, _ in provider.requests if "mutation" in r["query"]])
            saved_config, saved_installation = config.read_bytes(), installation.read_bytes()
            owner.kill()
            owner.wait(timeout=4)
        while time.time() <= deadline:
            time.sleep(0.03)
        request = {"action": "resume", "workflow_ref": reference, "operation_key": "t03-original"}
        command = [
            str(base / "bin/python"),
            "-I",
            str(bootstrap),
            provider.endpoint,
            str(base / "bin/aitm"),
            "--config",
            str(config),
            "--installation",
            str(installation),
            "--json",
            "workflow",
            "recover",
            "--input",
            "-",
        ]
        from ai_task_manager.adapters.decisions.observations import SessionObservations

        revoked = []

        def revoke_during_readback(message, result):
            if "query ReadIssue" in message["query"] and not revoked:
                revoked.append(True)
                current = read_private(config)
                SessionObservations(
                    authority, components.client, config, current["session_observations"]
                ).revoke()

        provider.hook = revoke_during_readback
        interrupted = subprocess.run(
            command,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=20,
            env={k: v for k, v in environment.items() if k != "PYTHONPATH"},
        )
        provider.hook = None
        write_private(
            root / "authority-loss-command.json",
            {
                "argv": command,
                "request": request,
                "returncode": interrupted.returncode,
                "stdout": interrupted.stdout,
                "stderr": interrupted.stderr,
            },
        )
        assert interrupted.returncode == 3 and revoked, interrupted.stdout or interrupted.stderr
        assert rig.current(task).sync_state != "confirmed"
        assert len([r for r, _ in provider.requests if "mutation" in r["query"]]) == sends
        # A fresh finite observation capability may settle the original effect;
        # it does not replace or un-revoke the original execution enrollment.
        observed = subprocess.run(
            command,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=20,
            env={k: v for k, v in environment.items() if k != "PYTHONPATH"},
        )
        value = json.loads(observed.stdout or observed.stderr)
        write_private(
            root / "installed-command.json",
            {
                "argv": command,
                "request": request,
                "returncode": observed.returncode,
                "stdout": observed.stdout,
                "stderr": observed.stderr,
            },
        )
        assert (
            observed.returncode == 4 and value["data"]["reason"] == "original_execution_unavailable"
        ), value
        assert value["data"]["provider_recovery"] == [
            {"operation_id": first["operation_id"], "state": "confirmed"}
        ]
        assert (
            rig.current(task).sync_state == "confirmed"
            and rig.current(task).observed_status == "In Progress"
        )
        assert len([r for r, _ in provider.requests if "mutation" in r["query"]]) == sends
        assert (
            config.read_bytes() == saved_config and installation.read_bytes() == saved_installation
        )
        assert rig.core.get_attempt(rig.handle, attempt.id).state == "starting"
        assert not (root / "state/native").exists() or not list((root / "state/native").iterdir())
        report = os.environ.get("T03_INSTALLED_REPORT")
        if report:
            with Path(report).open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "fixture": str(root),
                            "owner_pid": owner.pid,
                            "owner_returncode": owner.returncode,
                            "workflow": reference,
                            "operation_id": first["operation_id"],
                            "grant_deadline": deadline,
                            "command": str(root / "installed-command.json"),
                            "mutations_before": sends,
                            "mutations_after": sends,
                            "authored_ports": True,
                            "native_launches": 0,
                        }
                    )
                    + "\n"
                )
    finally:
        if owner is not None and owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=4)
        provider.close()
        sockets.cleanup()


@pytest.mark.parametrize("known", [True, False])
def test_t03_pending_confirmation_uses_readback_without_another_author(tmp_path, known):
    from ai_task_manager.adapters.linear.errors import LinearError
    from tests.workflows.helpers import WorkflowFixture

    fixture = WorkflowFixture(tmp_path / "workflow")
    reads = []
    sends = []

    class RetainedProvider:
        def confirm_pending(self, handle, task_id):
            sends.append(task_id)
            raise LinearError("Authored lost acknowledgement", uncertain=True)

        def reconcile_pending(self, handle, task_id):
            reads.append(task_id)
            if not known:
                raise LinearError("Authored unknown mutation identity", uncertain=True)
            return fixture.rig.confirm(fixture.rig.current(fixture.task))

    try:
        first = fixture.new_driver(reviews=fixture.authored_reviews())
        first.provider = RetainedProvider()
        assert first.run(fixture.ref)["state"] == "uncertain"
        assert len(fixture.runtime.dispatches) == 1
        restarted = fixture.new_driver()
        restarted.provider = first.provider
        value = restarted.run(fixture.ref)
        assert value["state"] == ("done" if known else "uncertain"), value
        assert len(sends) == len(reads) == len(fixture.runtime.dispatches) == 1
    finally:
        fixture.close()
