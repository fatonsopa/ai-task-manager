"""Connection scope and durable delegation controls against owned HTTP."""

import json
from dataclasses import replace

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import IssueCreateCapability
from tests.linear.helpers import CONNECTION, EXPECTED, LinearRig, ProviderFixture, uid


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def mutations(rig):
    return [r for r, _ in rig.provider.requests if r["query"].startswith("mutation")]


def test_ac01_create_and_update_preserve_human_and_confirm_delegate(rig):
    task = rig.task()
    issue = rig.provider.issues[task.issue_id]
    assert issue["assignee"]["id"] == EXPECTED.viewer_id
    assert issue["delegate"]["id"] == CONNECTION.agent_id
    from ai_task_manager.domains.work.models import IssueStatus

    task = rig.transition(task, IssueStatus.TODO, "enqueue")
    values = mutations(rig)[-1]["variables"]["input"]
    assert values["assigneeId"] == EXPECTED.viewer_id
    assert values["delegateId"] == CONNECTION.agent_id
    snapshot = rig.client.read_issue(task.issue_id)
    assert snapshot.assignee_id == EXPECTED.viewer_id
    assert snapshot.delegate_id == CONNECTION.agent_id
    assert all(
        row["connection_digest"] == CONNECTION.digest for row in rig.journal.sends(rig.scope)
    )
    assert task.observed_status == IssueStatus.TODO
    assert task.execution_state == "idle"


@pytest.mark.parametrize(
    "case", ["missing", "workspace", "principal", "session", "human", "absent", "ambiguous"]
)
def test_ac02_refuses_invalid_connection_before_mutation(rig, case):
    rig.task(confirmed=False)
    selected = CONNECTION
    if case == "missing":
        rig.client.agent_connection = None
    elif case in {"workspace", "principal"}:
        selected = replace(CONNECTION, **{case + "_id": uid(90)})
    elif case == "session":
        selected = replace(CONNECTION, logical_session="another-session")
    elif case == "human":
        rig.provider.agent_users[0]["app"] = False
    elif case == "absent":
        rig.provider.agent_users.clear()
    elif case == "ambiguous":
        rig.provider.agent_users *= 2
    if case != "missing":
        rig.client.agent_connection = lambda: selected
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "failed", result
    assert not mutations(rig) and not rig.journal.sends(rig.scope)


def test_ac03_connection_replacement_fences_live_client(rig):
    rig.task()
    selected = replace(CONNECTION, connection_id=uid(99))
    rig.client.agent_connection = lambda: selected
    with pytest.raises(LinearError, match="agent_connection_changed"):
        rig.client.check_connection(rig.scope, refresh=True)
    assert len(mutations(rig)) == 1


def test_ac04_selected_connection_is_part_of_verified_binding(rig):
    rig.client.check_connection(rig.scope, refresh=True)
    assert rig.client.binding.connection == CONNECTION


def test_ac05_readback_delegate_race_remains_uncertain(rig):
    task = rig.task(confirmed=False)

    def changed(request, result):
        if "mutation CreateIssue" in request["query"]:
            issue_id = result["issueCreate"]["issue"]["id"]
            rig.provider.edit(issue_id, delegate={"id": uid(92), "app": True})

    rig.provider.hook = changed
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain" and len(mutations(rig)) == 1
    assert rig.current(task).issue_id is None
    with pytest.raises(LinearError):
        rig.reconcile.recover_outbound(rig.handle, result["operation_id"])
    assert len(mutations(rig)) == 1


def test_ac06_lost_create_response_recovers_same_uuid_and_connection_only(rig, monkeypatch):
    from tests.linear.test_capability_and_omissions import schema

    rig.client.create_capability = IssueCreateCapability.from_introspection(schema())
    task = rig.task(confirmed=False)
    original = rig.client.create_issue

    def lost(task, candidate_id, **kwargs):
        original(task, candidate_id, **kwargs)
        raise LinearError("transport_failure", uncertain=True)

    monkeypatch.setattr(rig.client, "create_issue", lost)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "uncertain"
    assert rig.outbound.run_once(rig.handle) is None
    recovered = rig.reconcile.recover_outbound(rig.handle, result["operation_id"])
    assert recovered.id == task.id and recovered.issue_id == result["operation_id"]
    assert len(mutations(rig)) == 1


def test_ac07_restart_with_changed_connection_cannot_recover_old_send(rig):
    rig.task(confirmed=False)

    def failed_readback(request, result):
        if "mutation CreateIssue" in request["query"]:
            rig.provider.fail_next = (503, b"unavailable")

    rig.provider.hook = failed_readback
    result = rig.outbound.run_once(rig.handle)
    rig.provider.hook = None
    rig.client.connection = None  # New process has no remembered selection.
    rig.client.agent_connection = lambda: replace(CONNECTION, connection_id=uid(98))
    with pytest.raises(LinearError, match="agent_connection_changed"):
        rig.reconcile.recover_outbound(rig.handle, result["operation_id"])
    assert len(mutations(rig)) == 1


def test_ac08_two_users_cannot_reuse_one_selected_connection(rig):
    other = replace(rig.scope, principal=uid(88))
    with pytest.raises(LinearError, match="agent_connection_scope_mismatch"):
        rig.client.check_connection(other)
    other_session = replace(rig.scope, logical_session="second-logical-session")
    with pytest.raises(LinearError, match="agent_connection_scope_mismatch"):
        rig.client.check_connection(other_session)
    assert not mutations(rig)


def test_ac09_selected_app_need_not_support_agent_sessions(rig):
    rig.provider.agent_users[0]["supportsAgentSessions"] = False
    assert rig.client.check_connection(rig.scope, refresh=True) == CONNECTION
    rig.task()
    assert len(mutations(rig)) == 1


def test_ac10_frozen_inventory_matches_authored_controls():
    path = __import__("pathlib").Path(__file__).parent / "frozen" / "agent-connection-v1.json"
    data = json.loads(path.read_text())
    assert data["provider_evidence"] == "authored local HTTP only"
    assert len(data["cases"]) == 14


def test_ac11_durable_scope_refuses_replacement_before_first_send(rig):
    rig.task(confirmed=False)
    rig.client.connection = None
    rig.client.agent_connection = lambda: replace(CONNECTION, connection_id=uid(97))
    result = rig.outbound.run_once(rig.handle)
    assert result["error"] == "agent_connection_changed"
    assert result["state"] == "failed" and not mutations(rig)


def test_ac12_v3_journal_migration_preserves_sends_without_inventing_connection(tmp_path):
    import sqlite3

    from ai_task_manager.adapters.linear.receipts import SCHEMA, ProviderJournal

    path = tmp_path / "old-provider.sqlite"
    old_schema = SCHEMA.replace("INSERT INTO metadata VALUES(4)", "INSERT INTO metadata VALUES(3)")
    old_schema = old_schema.replace(
        "CREATE TABLE agent_bindings(scope TEXT PRIMARY KEY, connection_digest TEXT NOT NULL);", ""
    ).replace(", connection_digest TEXT", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO sends VALUES(?,?,?,?,?,?,?,?)",
            (uid(70), "{}", uid(71), 1, "uncertain", None, "transport_failure", uid(72)),
        )
    journal = ProviderJournal(path)
    with journal.transaction() as conn:
        assert conn.execute("SELECT version FROM metadata").fetchone()[0] == 4
        row = conn.execute("SELECT * FROM sends").fetchone()
        assert row["state"] == "uncertain" and row["connection_digest"] is None
        assert row["candidate_id"] == uid(72)


def test_ac08_second_human_selects_own_verified_connection(rig):
    from types import SimpleNamespace

    from ai_task_manager.adapters.linear.client import LinearClient

    second_user = uid(86)
    rig.provider.viewer_id = second_user
    expected = replace(EXPECTED, viewer_id=second_user)
    connection = replace(
        CONNECTION,
        connection_id=uid(85),
        principal_id=second_user,
        logical_session="second-human-session",
    )
    client = LinearClient(
        rig.client.transport, rig.client.credentials, expected, agent_connection=lambda: connection
    )
    client.verify_binding()
    scope = replace(rig.scope, principal=second_user, logical_session=connection.logical_session)
    client.check_connection(scope, refresh=True)
    snapshot = client.create_issue(
        SimpleNamespace(title="Independent human task", body="Owned fixture.")
    )
    assert snapshot.assignee_id == second_user and snapshot.delegate_id == connection.agent_id
    with pytest.raises(LinearError):
        rig.client.check_connection(scope)
    assert len(mutations(rig)) == 1


def test_ac13_first_authorized_import_update_establishes_selected_delegate(tmp_path):
    from ai_task_manager.application.linear_sync.outbound import OutboundDrain
    from tests.existing.helpers import ExistingProvider, ExistingRig

    provider = ExistingProvider()
    try:
        rig = ExistingRig(tmp_path, provider)
        issue_id = provider.seed()
        task = rig.queue(rig.admit(issue_id))
        assert not provider.issues[issue_id].get("delegate")
        confirmed = OutboundDrain(rig.core, rig.client, rig.journal).confirm_pending(
            rig.handle, task.id
        )
        assert confirmed.observed_status == "Todo"
        assert provider.issues[issue_id]["delegate"]["id"] == CONNECTION.agent_id
        assert provider.issues[issue_id]["assignee"]["id"] == EXPECTED.viewer_id
    finally:
        provider.close()


def test_ac14_confirmed_delegate_removal_refuses_update(rig):
    from ai_task_manager.domains.work.models import IssueStatus

    task = rig.task()
    rig.provider.edit(task.issue_id, delegate=None)
    rig.transition(task, IssueStatus.TODO, "enqueue", confirm=False)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "failed" and result["error"] == "preflight_conflict"
    assert len(mutations(rig)) == 1
