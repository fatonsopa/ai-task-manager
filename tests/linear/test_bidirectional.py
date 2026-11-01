import pytest

from ai_task_manager.domains.work.errors import InvalidTransition
from ai_task_manager.domains.work.models import IssueStatus as S
from tests.linear.helpers import STATES, LinearRig, ProviderFixture, uid


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        yield LinearRig(tmp_path, provider)
    finally:
        provider.close()


def test_l01_l05_l08_real_http_intake_queue_and_verified_worker_chain(rig):
    task = rig.task(confirmed=False)
    assert not rig.provider.issues and task.issue_id is None
    task = rig.confirm(task)
    assert task.observed_status == S.BACKLOG
    create = next(
        request
        for request, _ in rig.provider.requests
        if "mutation CreateIssue" in request["query"]
    )
    assert create["variables"]["input"]["stateId"] == STATES["Backlog"]
    assert create["variables"]["input"]["description"] == task.body
    task = rig.transition(task, S.TODO, "enqueue")
    task, attempt = rig.running(task)
    assert task.observed_status == S.IN_PROGRESS and attempt.state == "running"
    assert len(rig.provider.issues) == 1
    assert all(
        headers["Authorization"] == "fixture-api-secret" for _, headers in rig.provider.requests
    )


def test_l17_l27_signed_human_todo_applies_immediately_without_reconciliation(rig):
    task = rig.task()
    rig.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    event = rig.provider.event(task.issue_id)
    headers, body = rig.signed(event)
    assert rig.receiver.receive("POST", headers, body).status == 200
    assert rig.current(task).observed_status == S.BACKLOG
    outcome = rig.inbound.run_once(rig.handle)
    assert outcome["state"] == "applied"
    task = rig.current(task)
    assert task.observed_status == S.TODO and task.queued_once and not task.human_hold
    assert rig.core.notifications.list_events(rig.handle)[-1].kind == "external_change_applied"
    assert {row["state"] for row in rig.journal.deliveries(rig.scope)} == {"applied"}


def test_l21_human_hold_fences_old_intent_and_revokes_work_before_stop(rig):
    task, attempt = rig.running()
    progress = rig.core.execution.checkpoint(
        rig.handle, attempt.id, attempt.version, "progress", {}, rig.key()
    )
    assert progress
    rig.provider.edit(task.issue_id, state={"id": STATES["On Hold"], "name": "On Hold"})
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    assert rig.receiver.receive("POST", headers, body).status == 200
    result = rig.inbound.run_once(rig.handle)
    assert result["state"] == "conflict"
    task = rig.current(task)
    assert task.human_hold and task.observed_status == S.IN_PROGRESS
    assert rig.core.get_attempt(rig.handle, attempt.id).state == "running"
    with pytest.raises(InvalidTransition):
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)


def test_l21_pending_todo_is_fenced_atomically_by_new_human_backlog_edit(rig):
    task = rig.task()
    rig.transition(task, S.TODO, "enqueue", confirm=False)
    old_operation = rig.core.sync.list_operations(rig.handle)[-1]
    rig.provider.edit(task.issue_id, title=task.title)
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    assert rig.receiver.receive("POST", headers, body).status == 200
    result = rig.inbound.run_once(rig.handle)
    assert result["state"] == "applied"
    current = rig.current(task)
    assert current.observed_status == S.BACKLOG and current.desired_status == S.BACKLOG
    fenced = next(x for x in rig.core.sync.list_operations(rig.handle) if x.id == old_operation.id)
    assert fenced.fenced_by and fenced.state == "pending"
    assert rig.outbound.run_once(rig.handle) is None


def test_l18_remote_inprogress_does_not_launch_or_admit_a_worker(rig):
    task = rig.queued()
    rig.provider.edit(task.issue_id, state={"id": STATES["In Progress"], "name": "In Progress"})
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    assert rig.current(task).human_hold and rig.current(task).observed_status == S.TODO
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_l20_exact_outbound_echo_does_not_increment_decision_or_write_again(rig):
    task = rig.queued()
    before = len(rig.provider.requests)
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    rig.receiver.receive("POST", headers, body)
    result = rig.inbound.run_once(rig.handle)
    assert result["classification"] == "outbound_echo"
    assert rig.current(task).decision_revision == 0
    assert len(rig.provider.requests) == before + 1
    assert len(rig.core.sync.list_operations(rig.handle)) == 2


def test_l16_assignment_away_revokes_owned_work_and_does_not_import_foreign_tasks(rig):
    task, attempt = rig.running()
    rig.provider.edit(task.issue_id, assignee={"id": uid(90)})
    headers, body = rig.signed(rig.provider.event(task.issue_id))
    assert rig.receiver.receive("POST", headers, body).status == 200
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    assert rig.current(task).human_hold
    assert len(rig.core.list_tasks(rig.handle)) == 1


def test_l19_untrusted_integration_cannot_mint_human_decision(rig):
    task = rig.task()
    rig.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    headers, body = rig.signed(
        rig.provider.event(task.issue_id, actor={"type": "integration", "id": uid(88)})
    )
    rig.receiver.receive("POST", headers, body)
    assert rig.inbound.run_once(rig.handle)["state"] == "conflict"
    assert rig.current(task).human_hold and rig.current(task).decision_revision == 0


def test_l09_preflight_detects_unobserved_human_change_before_mutation(rig):
    task = rig.task()
    task = rig.transition(task, S.TODO, "enqueue", confirm=False)
    rig.provider.edit(task.issue_id, state={"id": STATES["On Hold"], "name": "On Hold"})
    before = sum("mutation UpdateIssue" in request["query"] for request, _ in rig.provider.requests)
    result = rig.outbound.run_once(rig.handle)
    assert result["state"] == "failed" and result["error"] == "preflight_conflict"
    assert (
        sum("mutation UpdateIssue" in request["query"] for request, _ in rig.provider.requests)
        == before
    )
    assert rig.current(task).human_hold
