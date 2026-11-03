"""Installed production follow-up path with authored HTTP and writing-review contracts."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.installation.sessions import setup
from tests.application.helpers import FEATURE_BODY, reviewed_control
from tests.linear.helpers import CONNECTION, EXPECTED, STATES, ProviderFixture
from tests.module_core.fixture import CLI


class FollowupRig:
    def __init__(self, root, writer_root, actions):
        self.root = root
        root.chmod(0o700)
        self.provider = ProviderFixture()
        try:
            package = WriterPackage.capture(writer_root, sys.executable)
            credential = root / "credential.env"
            credential.write_text("LINEAR_API_KEY=authored-local-contract-only\n")
            credential.chmod(0o600)
            self.linear = {
                "expected": asdict(EXPECTED),
                "agent_connection": asdict(CONNECTION),
                "credential_file": str(credential),
                "credential_key": "LINEAR_API_KEY",
                "journal": str(root / "provider.sqlite"),
            }
            self.config = root / "service.json"
            write_private(
                self.config,
                {
                    "version": 1,
                    "core_database": str(root / "core.sqlite"),
                    "authority_database": str(root / "authority.sqlite"),
                    "authority_key": str(root / "authority.key"),
                    "session_file": str(root / "session.json"),
                    "approval_policy": asdict(package.policy()),
                    "linear": self.linear,
                    "writer": {
                        "root": str(package.root),
                        "python": package.python,
                        "pins": package.pins,
                        "sessions_root": str(root / "writing"),
                        "journal": str(root / "writing.sqlite"),
                        "key": str(root / "writing.key"),
                    },
                },
            )
            self.deadline = time.time() + 1800
            setup(
                self.config,
                logical_session=CONNECTION.logical_session,
                actions=actions,
                deadline=self.deadline,
                operation_key="followup-setup",
                components=self.components(),
            )
            self.service = self.reload()
            self.writing_ref = self.approve("Investigate the new report")
            self.parent = self.submit_parent()
        except BaseException:
            self.close()
            raise

    def components(self):
        return LinearComponents(
            self.linear,
            client_factory=lambda credentials, expected: LinearClient(
                HttpTransport(_test_endpoint=self.provider.endpoint), credentials, expected
            ),
        )

    def reload(self):
        return load_service(self.config, linear_components=self.components())

    def approve(self, title):
        prepared = self.service.call(
            "writing_prepare",
            {
                "kind": "issue_feature",
                "purpose": "task",
                "title": title,
                "body": FEATURE_BODY,
                "code_bearing": False,
                "required_work": ["complete"],
            },
        )
        assert prepared["ok"], prepared
        item = prepared["data"]
        reviewed = self.service.call(
            "writing_review",
            {
                "writing_id": item["writing_id"],
                "review": reviewed_control(item["packet"]),
            },
        )
        assert reviewed["ok"] and reviewed["data"]["accepted"], reviewed
        finalized = self.service.call("writing_finalize", {"writing_id": item["writing_id"]})
        assert finalized["ok"], finalized
        return finalized["data"]["approval_ref"]

    def submit_parent(self):
        ref = self.approve("Retain the original delivered work")
        result = self.service.call("task_submit", {"writing_ref": ref, "operation_key": "parent"})
        assert result["ok"], result
        return self.service.core.get_task(self.service.handle, result["data"]["id"])

    def observe_delivery(self, *, foreign=False):
        s, parent = self.service, self.parent
        changes = {"state": {"id": STATES["Done"], "name": "Done"}}
        if foreign:
            changes["assignee"] = {"id": str(uuid4())}
        self.provider.edit(parent.issue_id, **changes)
        snapshot = s.inbound.client.read_issue(parent.issue_id)
        reference = s.inbound.journal.external_receipt(
            s.scope, parent, snapshot, self.provider.event(parent.issue_id), {EXPECTED.viewer_id}
        )
        observed = s.core.external.observe(
            s.handle,
            parent.id,
            parent.version,
            parent.decision_revision,
            reference,
            "observed-delivery",
        )
        assert observed["state"] == "pending"
        self.parent = s.core.get_task(s.handle, parent.id)
        assert self.parent.delivered_once
        # This is an observed canonical fence, not a fabricated execution or settlement.
        assert self.parent.sync_state == "uncertain"

    def arguments(self, **changes):
        return {
            "writing_ref": self.writing_ref,
            "parent_task_id": self.parent.id,
            "operation_key": "followup",
            **changes,
        }

    def effects(self):
        with sqlite3.connect(
            "file:" + str(self.root / "core.sqlite") + "?mode=ro", uri=True
        ) as conn:
            state = {
                name: conn.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall()
                for name in ("tasks", "events", "outbox", "operations", "attempts")
            }
        return state, sum("mutation " in request[0]["query"] for request in self.provider.requests)

    def cli(self, data, mode="normal"):
        command = [
            sys.executable,
            "-I",
            "-c",
            CLI,
            self.provider.endpoint,
            mode,
            "--config",
            str(self.config),
            "--json",
            "task",
            "submit",
            "--input",
            "-",
        ]
        completed = subprocess.run(
            command, input=json.dumps(data), capture_output=True, text=True, timeout=30
        )
        with (self.root / "cli.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "command": command,
                        "input": data,
                        "exit": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    }
                )
                + "\n"
            )
        return completed

    def proof(self):
        with self.service.decisions.journal.transaction() as conn:
            rows = [json.loads(row[0]) for row in conn.execute("SELECT context FROM invocations")]
        return next(row["delivered_followup"] for row in rows if "delivered_followup" in row)

    def close(self):
        self.provider.close()
        assert not self.provider.thread.is_alive()
        (self.root / "cleanup.json").write_text(
            json.dumps({"provider_thread_stopped": True, "native_workers_started": 0})
        )


@pytest.fixture
def rig(tmp_path, request):
    writer = os.environ.get("TECHNICAL_WRITER_PACKAGE")
    if not writer:
        pytest.skip("Configured external Technical Writer required")
    actions = getattr(request, "param", ("enqueue", "delivered_followup"))
    value = FollowupRig(tmp_path, writer, actions)
    try:
        yield value
    finally:
        value.close()


def test_installed_cli_followup_lost_reply_and_explicit_proof_replay(rig):
    import ai_task_manager

    assert "/site-packages/" in ai_task_manager.__file__, (
        "Run with the installed candidate interpreter"
    )
    rig.observe_delivery()
    parent = rig.parent
    original_provider_parent = json.loads(json.dumps(rig.provider.issues[parent.issue_id]))
    result = rig.cli(rig.arguments(), mode="drop-reply")
    assert result.returncode == 70, result.stderr
    assert len(rig.provider.issues) == 2
    before = rig.effects()
    replay = rig.cli(rig.arguments())
    assert replay.returncode == 0, replay.stderr
    child = json.loads(replay.stdout)["data"]
    assert rig.effects() == before
    assert child["parent_task_id"] == parent.id and child["id"] != parent.id
    assert child["observed_status"] == child["desired_status"] == "Backlog"
    assert child["sync_state"] == "confirmed"
    assert child["queued_once"] is child["delivered_once"] is False
    assert child["execution_state"] == "idle" and child["scheduling"]["queue_sequence"] is None
    assert rig.service.core.get_task(rig.service.handle, parent.id) == parent
    assert rig.provider.issues[parent.issue_id] == original_provider_parent
    assert not rig.effects()[0]["attempts"]
    conflict = rig.cli(rig.arguments(writing_ref=parent.approval_ref))
    assert conflict.returncode == 2
    assert json.loads(conflict.stderr)["error"]["category"] == "invalid_request"
    assert rig.effects() == before
    # Explicit references retain proof semantics and use the same production router.
    explicit = rig.cli(rig.arguments(operation_key="explicit-proof", followup_ref=rig.proof()))
    assert explicit.returncode == 0, explicit.stderr
    assert json.loads(explicit.stdout)["data"]["parent_task_id"] == parent.id
    assert len(rig.provider.issues) == 3
    rig.service.decisions.observations.revoke()
    committed = rig.cli(rig.arguments())
    assert committed.returncode == 0 and json.loads(committed.stdout)["data"]["id"] == child["id"]
    assert len(rig.provider.issues) == 3


@pytest.mark.parametrize(
    "fault",
    [
        "ineligible",
        "foreign_parent",
        "unknown_proof",
        "grant_as_proof",
        "wrong_prose",
        "revoked",
        "expired",
        "foreign_scope",
        "unapproved",
        "quarantined_parent",
        "wrong_parent_proof",
    ],
)
def test_followup_refusals_preserve_core_and_provider_effects(rig, fault):
    if fault != "ineligible":
        rig.observe_delivery(foreign=fault == "quarantined_parent")
    data = rig.arguments()
    if fault == "foreign_parent":
        data["parent_task_id"] = str(uuid4())
    elif fault == "unknown_proof":
        data["followup_ref"] = "decision-proof:missing"
    elif fault == "grant_as_proof":
        data["followup_ref"] = rig.service.grant_ref
    elif fault == "unapproved":
        data["writing_ref"] = "writing-task:missing"
    elif fault == "wrong_parent_proof":
        from dataclasses import replace

        view = rig.service.writer.approvals_for_tasks(rig.service.scope)
        data["followup_ref"] = rig.service.decisions.delivered_followup(
            rig.service.scope,
            replace(rig.parent, id=str(uuid4())),
            view.verify_approval(rig.writing_ref).digest,
            grant_ref=rig.service.grant_ref,
        )
    elif fault in ("wrong_prose", "foreign_scope"):
        proof = rig.service.decisions.delivered_followup(
            rig.service.scope, rig.parent, "0" * 64, grant_ref=rig.service.grant_ref
        )
        data["followup_ref"] = proof
        if fault == "foreign_scope":
            from dataclasses import replace

            rig.service.scope = replace(rig.service.scope, logical_session="foreign")
    elif fault == "revoked":
        rig.service.decisions.observations.revoke()
    elif fault == "expired":
        rig.service.decisions.journal.clock = lambda: rig.deadline
    before = rig.effects()
    result = rig.service.call("task_submit", data)
    assert not result["ok"], result
    category = "invalid_request" if fault == "foreign_scope" else "denied"
    assert result["outcome"] == result["error"]["category"] == category, result
    assert result["exit_code"] == (2 if fault == "foreign_scope" else 3), result
    assert rig.effects() == before


@pytest.mark.parametrize("rig", [("enqueue",)], indirect=True)
def test_existing_enrollment_does_not_gain_followup_capability(rig):
    rig.observe_delivery()
    before = rig.effects()
    result = rig.service.call("task_submit", rig.arguments())
    assert not result["ok"] and "outside the enrolled" in str(result)
    assert rig.effects() == before


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_followup_revalidates_uncommitted_proof_after_revocation(
    rig, monkeypatch, committed
):
    rig.observe_delivery()
    service = rig.service
    original = service.confirm if committed else service.core.intake.submit
    target, name = (service, "confirm") if committed else (service.core.intake, "submit")

    def interrupt(*args, **kwargs):
        raise ConnectionError("authored interruption at the Core submission boundary")

    monkeypatch.setattr(target, name, interrupt)
    assert not service.call("task_submit", rig.arguments())["ok"]
    proof = rig.proof()
    assert proof.startswith("decision-proof:")
    monkeypatch.setattr(target, name, original)
    service.decisions.observations.revoke()
    before = rig.effects()
    retry = rig.reload().call("task_submit", rig.arguments())
    assert retry["ok"] is committed, retry
    if committed:
        assert len(rig.provider.issues) == 2
        assert len(rig.effects()[0]["tasks"]) == len(before[0]["tasks"]) == 2
    else:
        assert "revoked" in str(retry)
        assert rig.effects() == before


@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("drift", ["credential_path", "configuration_path"])
def test_cold_followup_source_continuity_preserves_only_committed_replay(
    rig, monkeypatch, supplied, committed, drift
):
    rig.observe_delivery()
    service = rig.service
    original_submit = service.core.intake.submit

    def interrupt(*args, **kwargs):
        raise ConnectionError("authored interruption before the next submission boundary")

    monkeypatch.setattr(service.core.intake, "submit", interrupt)
    assert not service.call("task_submit", rig.arguments())["ok"]
    reference = rig.proof()
    monkeypatch.setattr(service.core.intake, "submit", original_submit)
    data = (
        rig.arguments(operation_key="supplied", followup_ref=reference)
        if supplied
        else rig.arguments()
    )
    if committed:
        original_confirm = service.confirm
        monkeypatch.setattr(service, "confirm", interrupt)
        assert not service.call("task_submit", data)["ok"]
        monkeypatch.setattr(service, "confirm", original_confirm)
        assert len(rig.effects()[0]["tasks"]) == 2
    before = rig.effects()
    config = json.loads(rig.config.read_text())
    if drift == "credential_path":
        replacement = rig.root / "same-account-new-source.env"
        replacement.write_bytes(Path(rig.linear["credential_file"]).read_bytes())
        replacement.chmod(0o600)
        config["linear"]["credential_file"] = str(replacement)
        write_private(rig.config, config, replace=True)
        rig.linear = config["linear"]
    else:
        rig.config = rig.root / "copied-config.json"
        write_private(rig.config, config)
    response = rig.cli(data)
    if committed:
        assert response.returncode == 0, response.stderr
        child = json.loads(response.stdout)["data"]
        assert child["parent_task_id"] == rig.parent.id
        assert child["observed_status"] == "Backlog" and not child["queued_once"]
        assert len(rig.effects()[0]["tasks"]) == len(before[0]["tasks"]) == 2
        assert len(rig.provider.issues) == 2
        settled = rig.effects()
        replay = rig.cli(data)
        assert replay.returncode == 0
        assert json.loads(replay.stdout)["data"]["id"] == child["id"]
        assert rig.effects() == settled
        conflict = rig.cli({**data, "writing_ref": rig.parent.approval_ref})
        assert conflict.returncode == 2
        assert json.loads(conflict.stderr)["outcome"] == "invalid_request"
        assert rig.effects() == settled
    else:
        assert response.returncode == 3, response.stdout + response.stderr
        error = json.loads(response.stderr)
        assert error["outcome"] == "denied"
        assert "enrollment configuration or source changed" in error["error"]["message"]
        assert rig.effects() == before
    assert rig.service.core.get_task(rig.service.handle, rig.parent.id) == rig.parent
    assert not rig.effects()[0]["attempts"]


def test_followup_provider_verification_runs_outside_core_transaction(rig, monkeypatch):
    rig.observe_delivery()
    observations = rig.service.decisions.observations
    original = observations.verify_enrollment
    verified = []

    def check(reference):
        # A separate writer can acquire the Core lock while the actual provider
        # verification runs; this control does not write or fabricate Core data.
        with sqlite3.connect(rig.root / "core.sqlite", timeout=0) as probe:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        verified.append(reference)
        return original(reference)

    monkeypatch.setattr(observations, "verify_enrollment", check)
    result = rig.service.call("task_submit", rig.arguments())
    assert result["ok"], result
    assert verified and set(verified) == {rig.service.grant_ref}


def test_committed_submit_recovery_retains_exact_fingerprint_and_approval(rig):
    from ai_task_manager.domains.work.errors import ApprovalRequired, IdempotencyConflict

    rig.observe_delivery()
    result = rig.service.call("task_submit", rig.arguments())
    assert result["ok"], result
    service = rig.service
    view = service.writer.approvals_for_tasks(service.scope)
    prose = view.prose(rig.writing_ref)
    args = dict(
        handle=service.handle,
        prose=prose,
        approval_ref=rig.writing_ref,
        operation_key="followup",
        parent_task_id=rig.parent.id,
        followup_ref=rig.proof(),
    )
    service.decisions.observations.revoke()
    before = rig.effects()
    assert service.core.intake.recover_submit(**args).id == result["data"]["id"]
    for changes in (
        {"followup_ref": "decision-proof:changed"},
        {"parent_task_id": str(uuid4())},
        {"approval_ref": rig.parent.approval_ref, "prose": view.prose(rig.parent.approval_ref)},
    ):
        with pytest.raises(IdempotencyConflict):
            service.core.intake.recover_submit(**{**args, **changes})
    with pytest.raises(ApprovalRequired):
        service.core.intake.recover_submit(**{**args, "prose": view.prose(rig.parent.approval_ref)})
    assert rig.effects() == before
