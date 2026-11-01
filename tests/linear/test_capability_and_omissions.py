import ast
import hashlib
import json
from pathlib import Path

import pytest
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import IssueCreateCapability
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.adapters.linear.receipts import KINDS

from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import TaskProse
from tests.linear.helpers import STATES, LinearRig, ProviderFixture, uid


def schema():
    return {
        "name": "IssueCreateInput",
        "inputFields": [
            {"name": name, "type": {"kind": "SCALAR", "name": "String", "ofType": None}}
            for name in (
                "id",
                "title",
                "description",
                "teamId",
                "projectId",
                "assigneeId",
                "stateId",
            )
        ],
    }


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def test_l07_schema_capability_is_disabled_until_verified_shape(rig):
    task = rig.task(confirmed=False)
    assert rig.client.create_capability is None
    with pytest.raises(LinearError, match="creation_uuid_capability_missing"):
        rig.client.create_issue(task, uid(800))
    invalid = schema()
    invalid["inputFields"][0]["type"]["name"] = "Int"
    with pytest.raises(LinearError, match="unsupported_creation_uuid_capability"):
        IssueCreateCapability.from_introspection(invalid)


def test_l07_candidate_is_durable_before_send_and_exact_recovery_never_recreates(rig, monkeypatch):
    rig.client.create_capability = IssueCreateCapability.from_introspection(schema())
    rig.task(confirmed=False)
    original = rig.client.create_issue

    def lost(task, candidate_id=None):
        assert rig.journal.sends(rig.scope)[-1]["candidate_id"] == candidate_id
        original(task, candidate_id)
        raise LinearError("transport_failure", uncertain=True)

    monkeypatch.setattr(rig.client, "create_issue", lost)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain"
    candidate = rig.journal.sends(rig.scope)[-1]["candidate_id"]
    assert candidate == result["operation_id"] and candidate in rig.provider.issues
    confirmed = rig.reconcile.recover_outbound(rig.handle, result["operation_id"])
    assert confirmed.issue_id == candidate and len(rig.provider.issues) == 1


def test_l07_missing_or_wrong_candidate_readback_does_not_prove_absence(rig, monkeypatch):
    rig.client.create_capability = IssueCreateCapability.from_introspection(schema())
    task = rig.task(confirmed=False)
    monkeypatch.setattr(
        rig.client,
        "create_issue",
        lambda *args: (_ for _ in ()).throw(LinearError("transport_failure", uncertain=True)),
    )
    result = rig.outbound.run_once(rig.handle)
    with pytest.raises(LinearError, match="creation_identity_uncertain"):
        rig.reconcile.recover_outbound(rig.handle, result["operation_id"])
    with pytest.raises(EvidenceRequired):
        rig.reconcile.retry_unsent(rig.handle, result["operation_id"])
    assert not rig.provider.issues and rig.current(task).issue_id is None


def test_l07_returned_id_mismatch_retains_actual_identity_as_uncertain(rig, monkeypatch):
    rig.client.create_capability = IssueCreateCapability.from_introspection(schema())
    task = rig.task(confirmed=False)
    original = rig.provider.graphql

    def ignore_candidate(query, variables):
        if "mutation CreateIssue" in query:
            variables = {
                "input": {key: value for key, value in variables["input"].items() if key != "id"}
            }
        return original(query, variables)

    monkeypatch.setattr(rig.provider, "graphql", ignore_candidate)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain" and result["error"] == "creation_uuid_not_honored"
    send = rig.journal.sends(rig.scope)[0]
    assert json.loads(send["snapshot"])["id"] != send["candidate_id"]
    assert rig.current(task).issue_id is None and len(rig.provider.issues) == 1


def test_l06_renderer_preserves_commands_in_code_and_requires_exact_readback(rig):
    prose = TaskProse(
        "Verify list rendering",
        "- first\n\n- run `check --exact`\n\n```sh\n- literal --exact\n```\n    - indented code",
    )
    rendered = LinearMarkdownRenderer().render(prose)
    assert (
        rendered.body
        == "* first\n* run `check --exact`\n\n```sh\n- literal --exact\n```\n    - indented code"
    )
    task = rig.core.intake.submit(rig.handle, prose, rig.approval(prose), rig.key())

    def change(request, result):
        if "mutation CreateIssue" in request["query"]:
            issue = result["issueCreate"]["issue"]
            issue["description"] = issue["description"].replace("--exact", "--different")
            rig.provider.issues[issue["id"]]["description"] = issue["description"]

    rig.provider.hook = change
    assert rig.outbound.run_once(rig.handle)["state"] == "uncertain"
    assert rig.current(task).issue_id is None


def test_l18_unknown_remote_state_revokes_work_instead_of_becoming_idle(rig):
    task, attempt = rig.running()
    rig.provider.edit(task.issue_id, state={"id": uid(900), "name": "Unexpected"})
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    assert rig.current(task).human_hold
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "running"


def test_l19_older_event_cannot_authorize_newer_snapshot_then_signed_current_event_can(rig):
    task = rig.task()
    rig.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    older = rig.provider.event(task.issue_id)
    rig.provider.edit(task.issue_id, title=task.title)
    current = rig.provider.event(task.issue_id)
    headers, body = rig.signed(older)
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    assert rig.current(task).human_hold and rig.current(task).decision_revision == 0
    headers, body = rig.signed(current)
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["state"] == "applied"
    assert rig.current(task).observed_status == "Todo" and rig.current(task).decision_revision == 1


def test_l18_issue_removal_revokes_owned_work_without_claiming_cancellation(rig):
    task, attempt = rig.running()
    rig.provider.edit(task.issue_id, title=task.title)
    headers, body = rig.signed(rig.provider.event(task.issue_id, action="remove"))
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["error"] == "remote_issue_removed"
    assert rig.current(task).human_hold and rig.current(task).observed_status == "In Progress"
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "running"


def test_l24_provider_cannot_issue_decision_worker_writing_or_ai_authority(rig):
    assert not (
        {
            "decision",
            "human_decision",
            "worker_start",
            "resources_ready",
            "attempt_outcome",
            "notification_ai_acknowledged",
            "notification_user_reported",
            "approval",
        }
        & KINDS
    )
    for ref in ("runtime-proof:unknown", "linear-proof:unknown", "reviewed=True"):
        with pytest.raises(EvidenceRequired):
            rig.journal.verify_evidence(ref)


def test_l26_adapter_boundaries_do_not_copy_policy_or_write_core_tables():
    root = Path(__file__).parents[2] / "src" / "ai_task_manager"
    paths = [
        *root.joinpath("adapters", "linear").glob("*.py"),
        *root.joinpath("application", "linear_sync").glob("*.py"),
    ]
    forbidden_tables = {
        "tasks",
        "attempts",
        "capacity",
        "sessions",
        "operations",
        "events",
        "receipts",
        "outbox",
        "inbox",
        "external_observations",
    }
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                names = []
            assert not any(
                "legacy" in name
                or "linear_sync.py" in name
                or name.startswith(("subprocess", "ai_task_manager.storage"))
                for name in names
            )
            if isinstance(node, ast.Assign):
                assert not any(
                    isinstance(target, ast.Name) and target.id in {"_PAIRS", "ALLOWED_TRANSITIONS"}
                    for target in node.targets
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executescript"}
                and node.args
            ):
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    words = node.args[0].value.lower().replace("(", " ").replace(")", " ").split()
                    assert not (set(words) & forbidden_tables), (path, words)


def test_l28_frozen_case_manifests_and_live_plan_remain_truthful():
    root = Path(__file__).parent / "frozen"
    expected = {
        "linear-cases-v1.json": "169b4bc919d15e73ba838a8d6615fd2e95ae92401ae02eef1ab4cbd96926b627",
        "linear-cases-v2.json": "dc7d6838272411429e482684a67602c5348f2d1c2d8e8540e9a93710842dde3d",
        "core-import-evaluator-v2.json": (
            "2ea9d3cbcd6822a30f6a0de9f88b91cc2b6aec612f6e08acaeeed87ab157942e"
        ),
    }
    for name, wanted in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == wanted
    live = json.loads((root / "live-fixture-plan-v1.json").read_text())
    assert (
        live["execution_status"] == "not_run"
        and live["live_two_user"] == "blocked_missing_second_identity"
    )
    assert live["create_webhook_or_tunnel_now"] is False
