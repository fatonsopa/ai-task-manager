"""Default installed-source composition with authored HTTP and console input only."""

import os
import sys
import time
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.config.files import digest, read_private, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.sessions import setup
from ai_task_manager.installation.sessions.human import capture_instruction
from tests.application.helpers import FEATURE_BODY, reviewed_control
from tests.linear.helpers import CONNECTION, EXPECTED, uid
from tests.linear.test_decision_publication import DecisionProvider, fields

RECIPIENT = uid(800)
RECIPIENT_TOKEN = "AUTHORED_RECIPIENT_ONLY"


class CrossHumanProvider(DecisionProvider):
    def __init__(self):
        self.recipient_revoked = False
        super().__init__()

    def _fixture_query(self, query, variables):
        result = super()._fixture_query(query, variables)
        if "query VerifyBinding" in query:
            if self.requests[-1][1].get("Authorization") == RECIPIENT_TOKEN:
                result["viewer"]["id"] = EXPECTED.viewer_id if self.recipient_revoked else RECIPIENT
        if "query DecisionContext" in query:
            result["issue"]["creator"] = self.human(RECIPIENT)
        return result


def recipient_configuration(owner_config, root):
    root.mkdir(mode=0o700)
    owner = read_private(owner_config)
    credential = root / "credential.env"
    credential.write_text("LINEAR_TEST_USER_B_TOKEN=" + RECIPIENT_TOKEN + "\n")
    credential.chmod(0o600)
    connection = {
        **asdict(CONNECTION),
        "principal_id": RECIPIENT,
        "connection_id": str(uuid4()),
        "logical_session": "decision-recipient",
    }
    config = root / "service.json"
    write_private(
        config,
        {
            "version": 1,
            "core_database": str(root / "core.sqlite"),
            "authority_database": str(root / "authority.sqlite"),
            "authority_key": str(root / "authority.key"),
            "session_file": str(root / "session.json"),
            "approval_policy": owner["approval_policy"],
            "capacity_policies": owner.get("capacity_policies", []),
            "linear": {
                "expected": {**asdict(EXPECTED), "viewer_id": RECIPIENT},
                "agent_connection": connection,
                "credential_file": str(credential),
                "credential_key": "LINEAR_TEST_USER_B_TOKEN",
                "journal": str(root / "provider.sqlite"),
            },
        },
    )
    return config


def approve(service, prepared):
    assert prepared["mechanical_passed"], prepared["packet"]["mechanical_checks"]
    reviewed = service.call(
        "writing_review",
        {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
    )
    assert reviewed["ok"], reviewed
    final = service.call("writing_finalize", {"writing_id": prepared["writing_id"]})
    assert final["ok"], final
    return final["data"]["approval_ref"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    provider = CrossHumanProvider()
    monkeypatch.setattr(HttpTransport, "ENDPOINT", provider.endpoint)
    monkeypatch.setenv("AITM_CREDENTIAL_AUTHORED_OWNER", "AUTHORED_OWNER_ONLY")
    package = WriterPackage.capture(os.environ["TECHNICAL_WRITER_PACKAGE"], sys.executable)
    root = tmp_path / "owner"
    root.mkdir(mode=0o700)
    config = root / "service.json"
    write_private(
        config,
        {
            "version": 1,
            "core_database": str(root / "core.sqlite"),
            "authority_database": str(root / "authority.sqlite"),
            "authority_key": str(root / "authority.key"),
            "session_file": str(root / "session.json"),
            "approval_policy": asdict(package.verify()),
            "capacity_policies": [{"host": "codex", "host_limit": 1, "project_limit": 1}],
            "linear": {
                "expected": asdict(EXPECTED),
                "agent_connection": asdict(CONNECTION),
                "credential_environment": "AITM_CREDENTIAL_AUTHORED_OWNER",
                "journal": str(root / "provider.sqlite"),
            },
            "writer": {
                "root": str(package.root),
                "python": sys.executable,
                "pins": package.pins,
                "sessions_root": str(root / "writing"),
                "journal": str(root / "writing.sqlite"),
                "key": str(root / "writing.key"),
            },
        },
    )
    try:
        setup(
            config,
            logical_session=CONNECTION.logical_session,
            actions=("enqueue",),
            deadline=time.time() + 600,
            operation_key="owner-enrollment",
        )
        service = load_service(config)
        prepared = service.call(
            "writing_prepare",
            {
                "kind": "issue_feature",
                "purpose": "task",
                "title": "Fixture verification ownership",
                "body": FEATURE_BODY,
                "code_bearing": False,
                "required_work": ["verify"],
            },
        )
        assert prepared["ok"], prepared
        writing = approve(service, prepared["data"])
        created = service.call("task_submit", {"writing_ref": writing, "operation_key": "task"})
        assert created["ok"], created
        task = created["data"]
        prepared = service.call(
            "task_decision_prepare",
            {
                "task_id": task["id"],
                **fields(),
                "operation_key": "prepare-choice",
            },
        )
        assert prepared["ok"], prepared
        writing = approve(service, prepared["data"])
        request = service.call(
            "task_decision_request",
            {
                "task_id": task["id"],
                "writing_ref": writing,
                "operation_key": "request-choice",
            },
        )
        assert request["ok"], request
        recipient_config = recipient_configuration(config, tmp_path / "recipient")
        setup(
            recipient_config,
            logical_session="decision-recipient",
            actions=("enqueue",),
            deadline=time.time() + 600,
            operation_key="recipient-enrollment",
        )
        journal = HostJournal(root / "host.sqlite", root / "host.key")
        monkeypatch.setattr(
            OperatorChannel,
            "_exchange",
            lambda self, request, timeout: (
                {
                    "request_id": request["request_id"],
                    "challenge": request["challenge"],
                    "accepted": True,
                    "instruction_hash": digest(request["instruction"]),
                },
                os.getpid(),
            ),
        )
        yield SimpleNamespace(
            config=config,
            recipient_config=recipient_config,
            service=service,
            task=task,
            request=request["data"],
            journal=journal,
            provider=provider,
        )
    finally:
        provider.close()


def capture(f, **changes):
    task = f.service.core.get_task(f.service.handle, f.task["id"])
    args = dict(
        task_id=task.id,
        target=task.observed_status.value,
        expected_version=task.version,
        decision_revision=task.decision_revision,
        reason="Authored creator option",
        decision_request_id=f.request["id"],
        decision_option_id="A",
        recipient_config_file=f.recipient_config,
    )
    args.update(changes)
    return capture_instruction(f.config, f.journal, **args)


def resolve(f, reference):
    return load_service(f.config).call(
        "task_decision_resolve",
        {
            "request_id": f.request["id"],
            "human_ref": reference,
            "operation_key": "resolve-choice",
        },
    )


def test_creator_choice_uses_separately_authenticated_source_and_preserves_owner(env):
    f = env
    reference = capture(f)
    observed = load_service(f.config).decisions.observations.verify_instruction(reference)
    assert (
        observed.scope.principal == EXPECTED.viewer_id and observed.decision_actor_id == RECIPIENT
    )
    answer = resolve(f, reference)
    assert answer["ok"] and answer["data"]["decision_state"] == "resolved", answer
    owner = load_service(f.config)
    assert owner.scope.principal == EXPECTED.viewer_id
    assert owner.core.get_task(owner.handle, f.task["id"]).session_id == owner.session_id
    recipient = load_service(f.recipient_config)
    assert not recipient.call("task_show", {"task_id": f.task["id"]})["ok"]
    assert not recipient.call("task_decision_show", {"request_id": f.request["id"]})["ok"]


def test_owner_cannot_impersonate_creator_or_read_foreign_request(env):
    f = env
    for config in (None, f.config):
        with pytest.raises(AccessDenied):
            capture(f, recipient_config_file=config)
    with pytest.raises(Exception):
        capture(f, decision_request_id=str(uuid4()))
    assert f.journal._history("human") == []
    assert len(f.provider.comments) == 1


@pytest.mark.parametrize(
    "change", ["stale_task", "revoked", "config", "capability", "credential", "foreign_request"]
)
def test_stale_or_changed_recipient_source_refuses(env, change):
    f = env
    reference = capture(f)
    recipient = load_service(f.recipient_config)
    config = read_private(f.recipient_config)
    if change == "revoked":
        recipient.decisions.observations.revoke()
    elif change in {"config", "capability"}:
        path = f.recipient_config if change == "config" else config["session_file"]
        value = read_private(path)
        write_private(path, {**value, "changed_source": True}, replace=True)
    elif change == "credential":
        f.provider.recipient_revoked = True
    elif change == "foreign_request":
        f.request = {**f.request, "id": str(uuid4())}
    else:
        # Record a later real owner instruction through existing trusted producers/Core.
        task = f.service.core.get_task(f.service.handle, f.task["id"])
        owner_source = capture_instruction(
            f.config,
            f.journal,
            task_id=task.id,
            target="Todo",
            expected_version=task.version,
            decision_revision=task.decision_revision,
            reason="Authored later owner instruction",
        )
        plan = f.service.decisions.human_plan(
            f.service.scope, task, "Todo", "enqueue", owner_source
        )
        changed = f.service.core.decisions.record(
            f.service.handle,
            task.id,
            task.version,
            plan["human_decision_ref"],
            "later-owner-choice",
        )
        assert changed.version > task.version and changed.decision_revision > task.decision_revision
    before = list(f.provider.issues[f.task["issue_id"]].get("decision_labels", []))
    comments = len(f.provider.comments)
    answer = resolve(f, reference)
    assert not answer["ok"], answer
    assert f.provider.issues[f.task["issue_id"]].get("decision_labels", []) == before
    assert len(f.provider.comments) == comments
