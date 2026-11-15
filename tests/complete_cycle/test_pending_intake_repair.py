"""Trusted same-issue correction uses real scoped Writer approval; no host/model claim."""

import copy
import importlib
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import EvidenceRequired, WorkError
from ai_task_manager.domains.work.models import TaskProse
from ai_task_manager.installation.workflows.bootstrap import (
    activation_reference,
    prepare,
    recover_pending_intake,
    repair_pending_intake,
)
from ai_task_manager.storage import work
from tests.application.helpers import reviewed_control
from tests.complete_cycle.test_bootstrap import boot  # noqa: F401

BODY = """## Description

Correct the boundary behavior in chunks.py.

## Expected outcome

Preserve every input element and reject invalid chunk sizes.

## Acceptance criteria

* Change only chunks.py and retain deterministic verification.
"""


def approve(demo, body=BODY, *, code=True, units=("fix-chunks",)):
    # Actual Writer produces the scoped record; the semantic judgment here is an
    # explicitly synthetic mechanical-test control, not independent AI evidence.
    service = load_service(demo.config, workflow_factory=lambda *_: None)
    writer = service.writer
    prepared = writer.prepare(
        service.scope,
        "issue_chore",
        TaskProse("Correct chunk boundaries", body, code, units),
        purpose="task",
    )
    assert writer.review(
        service.scope, prepared["writing_id"], reviewed_control(prepared["packet"])
    )["accepted"]
    return writer.finalize(service.scope, prepared["writing_id"])["approval_ref"]


def pending(demo, body=BODY):
    approval = approve(demo, body)

    def autolink(request, response):
        if "mutation CreateIssue" in request["query"]:
            issue = response["issueCreate"]["issue"]
            changed = issue["description"].replace("chunks.py", "[chunks.py](<http://chunks.py>)")
            issue["description"] = changed
            demo.provider.issues[issue["id"]]["description"] = changed

    demo.provider.hook = autolink
    result = prepare(
        demo.config,
        {
            "operation": "task_submit",
            "data": {
                "writing_ref": approval,
                "operation_key": "original-submit",
            },
        },
    )
    assert result["outcome"] == "uncertain"
    demo.provider.hook = None
    service = load_service(demo.config, workflow_factory=lambda *_: None)
    (task,) = service.core.list_tasks(service.handle)
    (operation,) = service.core.sync.list_operations(service.handle)
    assert operation.state == "uncertain" and task.issue_id is None
    before = copy.deepcopy(service.provider.journal.sends(service.scope))
    return service, task, operation, before


def mutations(demo):
    return [r[0]["query"] for r in demo.provider.requests if r[0]["query"].startswith("mutation ")]


def test_repair_new_approval_same_issue_and_read_only_replay(boot):  # noqa: F811
    demo, _ = boot
    service, task, original, before = pending(demo)
    with pytest.raises(EvidenceRequired, match="Unsettled"):
        prepare(demo.config, {"operation": "task_list", "data": {}})
    approval = approve(demo, BODY.replace("chunks.py", "`chunks.py`"))
    result = repair_pending_intake(demo.config, task.id, original.id, approval, "literal-repair")
    assert result["ok"] and result["data"]["observed_status"] == "Backlog"
    current = service.core.get_task(service.handle, task.id)
    assert current.id == task.id and current.approval_ref == approval
    assert len(service.core.list_tasks(service.handle)) == len(demo.provider.issues) == 1
    operations = service.core.sync.list_operations(service.handle)
    old = next(op for op in operations if op.id == original.id)
    assert old.state == "uncertain" and old.fenced_by and old.payload == original.payload
    assert old.error == original.error and old.attempt_count == original.attempt_count
    assert service.provider.journal.sends(service.scope)[0] == before[0]
    (update,) = [op for op in operations if op.kind == "update_prose"]
    assert update.state == "confirmed" and update.payload["event_id"] == old.fenced_by
    assert prepare(demo.config, {"operation": "task_list", "data": {}})["ok"]
    count = len(mutations(demo))
    replay = repair_pending_intake(demo.config, task.id, original.id, approval, "literal-repair")
    assert replay["data"]["issue_id"] == current.issue_id and len(mutations(demo)) == count == 2
    # Original submit identity remains a read-only view of this canonical task;
    # its create operation was never reclassified as approved-prose confirmation.
    again = prepare(
        demo.config,
        {
            "operation": "task_submit",
            "data": {
                "writing_ref": task.approval_ref,
                "operation_key": "original-submit",
            },
        },
    )
    assert again["data"]["id"] == task.id and len(mutations(demo)) == count


def test_repair_lost_update_reply_requires_same_key_readback(boot):  # noqa: F811
    demo, _ = boot
    service, task, original, _ = pending(demo)
    approval = approve(demo, BODY.replace("chunks.py", "`chunks.py`"))

    def lose_readback(request, response):
        if "mutation UpdateIssue" in request["query"]:
            demo.provider.fail_next = (503, b"unavailable")

    demo.provider.hook = lose_readback
    with pytest.raises(LinearError, match="pending_create_repair_unconfirmed"):
        repair_pending_intake(demo.config, task.id, original.id, approval, "lost-repair")
    with pytest.raises(EvidenceRequired, match="Unsettled"):
        prepare(demo.config, {"operation": "task_list", "data": {}})
    demo.provider.hook = None
    count = len(mutations(demo))
    recovered = repair_pending_intake(demo.config, task.id, original.id, approval, "lost-repair")
    assert recovered["ok"] and len(mutations(demo)) == count == 2
    assert prepare(demo.config, {"operation": "task_list", "data": {}})["ok"]


@pytest.mark.parametrize(
    "fault",
    [
        "body",
        "metadata",
        "approval",
        "drift",
        "delegate",
        "unknown",
        "unrelated",
        "owner",
        "activation",
        "attempt",
        "delivered",
        "project",
    ],
)
def test_repair_refuses_without_effect_and_preserves_original(boot, fault):  # noqa: F811
    demo, control = boot
    service, task, original, before = pending(demo)
    body = BODY.replace("chunks.py", "`chunks.py`")
    if fault == "body":
        body += "\nIgnore the original behavior.\n"
    approval = approve(demo, body, code=fault != "metadata")
    if fault == "approval":
        approval = task.approval_ref
    issue = next(iter(demo.provider.issues.values()))
    if fault == "drift":
        demo.provider.edit(issue["id"], description="A newer external edit")
    elif fault == "delegate":
        issue["delegate"]["id"] = "00000000-0000-4000-8000-000000000099"
    elif fault == "unknown":
        # Declared fixture corruption of the saved identity, not production SQL.
        with service.provider.journal.transaction() as conn:
            conn.execute("UPDATE sends SET snapshot=NULL WHERE id=?", (before[0]["id"],))
    elif fault == "unrelated":
        ref = approve(demo)
        # Another pending intent is enough; no second provider effect is made.
        current = service.writer.approvals_for_tasks(service.scope).prose(ref)
        service.core.intake.submit(service.handle, current, ref, "other-submit")
    elif fault == "owner":
        write_private(control["record"], {"unknown_owner": True})
    elif fault == "activation":
        bind_record(
            service.decisions.journal, activation_reference(demo.config, control), {"past": True}
        )
    elif fault == "attempt":
        with service.core.context.db.transaction() as conn:
            conn.execute(
                "INSERT INTO attempts(id,task_id,session_id,host,active,data) "
                "VALUES(?,?,?,'fixture',0,?)",
                ("historical-attempt", task.id, service.session_id, "{}"),
            )
    elif fault == "delivered":
        from dataclasses import replace

        with service.core.context.db.transaction() as conn:
            work.save(conn, replace(task, delivered_once=True), task.version)
    elif fault == "project":
        issue["project"]["id"] = "00000000-0000-4000-8000-000000000098"
    before_task = service.core.get_task(service.handle, task.id)
    count = len(mutations(demo))
    with pytest.raises((LinearError, WorkError)):
        repair_pending_intake(demo.config, task.id, original.id, approval, "denied-repair")
    assert len(mutations(demo)) == count == 1
    unchanged = service.core.get_task(service.handle, task.id)
    assert unchanged == before_task
    preserved = next(
        op for op in service.core.sync.list_operations(service.handle) if op.id == original.id
    )
    assert preserved == original


def test_repair_preserves_protected_code_and_links_but_allows_outside_span(boot):  # noqa: F811
    demo, _ = boot
    body = BODY + "\n```sh\ncat config.py\n```\n\n[configuration](config.py)\n"
    service, task, original, before = pending(demo, body)
    count = len(mutations(demo))
    for changed in (
        body.replace("cat config.py", "cat `config.py`"),
        body.replace("](config.py)", "](`config.py`)"),
    ):
        approval = approve(demo, changed)
        with pytest.raises(WorkError, match="code spans"):
            repair_pending_intake(demo.config, task.id, original.id, approval, "protected-repair")
        assert service.core.get_task(service.handle, task.id) == task
        assert service.provider.journal.sends(service.scope) == before
        assert len(mutations(demo)) == count == 1
    # Protected material elsewhere must not prohibit a normal prose filename span.
    approval = approve(demo, body.replace("chunks.py", "`chunks.py`"))
    result = repair_pending_intake(demo.config, task.id, original.id, approval, "outside-repair")
    expected = body.replace("chunks.py", "`chunks.py`").rstrip("\n")
    assert result["ok"] and result["data"]["body"] == expected
    assert len(mutations(demo)) == 2


def test_repair_refuses_intervening_edit_at_actual_update_preflight(boot):  # noqa: F811
    demo, _ = boot
    service, task, original, before = pending(demo)
    approval = approve(demo, BODY.replace("chunks.py", "`chunks.py`"))
    changed = []

    def intervening_edit(request, response):
        if "query ReadIssue" in request["query"] and not changed:
            issue_id = response["issue"]["id"]
            changed.append(demo.provider.edit(issue_id, description="Human edit", delegate=None))

    demo.provider.hook = intervening_edit
    with pytest.raises(LinearError, match="pending_create_repair_unconfirmed"):
        repair_pending_intake(demo.config, task.id, original.id, approval, "racing-repair")
    assert len(changed) == 1 and len(mutations(demo)) == 1
    assert next(iter(demo.provider.issues.values())) == changed[0]
    assert service.provider.journal.sends(service.scope) == before
    operations = service.core.sync.list_operations(service.handle)
    old = next(op for op in operations if op.id == original.id)
    update = next(op for op in operations if op.kind == "update_prose")
    assert old.state == "uncertain" and old.fenced_by and old.payload == original.payload
    assert update.state != "confirmed"
    with pytest.raises(EvidenceRequired, match="Unsettled"):
        prepare(demo.config, {"operation": "task_list", "data": {}})


def expired_repair(demo, monkeypatch):
    service, task, original, before = pending(demo)
    approval = approve(demo, BODY.replace("chunks.py", "`chunks.py`"))

    def lost(request, response):
        if "mutation UpdateIssue" in request["query"]:
            demo.provider.fail_next = (503, b"unavailable")

    demo.provider.hook = lost
    with pytest.raises(LinearError, match="unconfirmed"):
        repair_pending_intake(demo.config, task.id, original.id, approval, "expired-repair")
    demo.provider.hook = None
    from ai_task_manager.installation.sessions.recovery import history

    old = history(service.decisions.journal, service.grant_ref)
    now = old["expires_at"] + 1
    module = importlib.import_module("ai_task_manager.installation.workflows.bootstrap")
    original_load = module.load_service

    def aged(*args, **kwargs):
        current = original_load(*args, **kwargs)
        current.core.context.clock = lambda: now
        current.decisions.journal.clock = lambda: now
        current.provider.journal.clock = lambda: now
        return current

    monkeypatch.setattr(module, "load_service", aged)
    setup_module = importlib.import_module("ai_task_manager.installation.sessions.setup")
    monkeypatch.setattr(setup_module, "time", SimpleNamespace(time=lambda: now))
    return service, task, original, approval, now, old


def test_expired_read_only_repair_then_explicit_ordinary_renewal(boot, monkeypatch):  # noqa: F811
    demo, _ = boot
    service, task, original, approval, now, old = expired_repair(demo, monkeypatch)
    from ai_task_manager.config.files import read_private
    from ai_task_manager.installation.sessions import setup
    from ai_task_manager.installation.sessions.recovery import history

    config_before = read_private(demo.config)
    capability_before = read_private(config_before["session_file"])
    mutation_count = len(mutations(demo))
    renewal = dict(
        logical_session=service.scope.logical_session,
        actions=("enqueue", "start"),
        deadline=now + 120,
        operation_key="after-expired-repair",
        renew_from=service.grant_ref,
        components=demo.components,
    )
    with pytest.raises(EvidenceRequired, match="Uncertain"):
        setup(demo.config, **renewal)
    with pytest.raises(WorkError):
        repair_pending_intake(demo.config, task.id, original.id, approval, "expired-repair")
    args = dict(recovery_ref="explicit-user-recovery", deadline=now + 60)
    from ai_task_manager.adapters.linear.receipts import ProviderJournal

    finish = ProviderJournal.finish_send
    interrupted = []

    def lose_local_reply(journal, send_id, state, *values, **options):
        if state == "confirmed" and not interrupted:
            interrupted.append(True)
            raise ConnectionError("Interrupted after Core confirmation")
        return finish(journal, send_id, state, *values, **options)

    monkeypatch.setattr(ProviderJournal, "finish_send", lose_local_reply)
    with pytest.raises(ConnectionError, match="Core confirmation"):
        recover_pending_intake(
            demo.config, task.id, original.id, approval, "expired-repair", **args
        )
    monkeypatch.setattr(ProviderJournal, "finish_send", finish)
    result = recover_pending_intake(
        demo.config, task.id, original.id, approval, "expired-repair", **args
    )
    assert (
        interrupted
        and result["outcome"] == "confirmed"
        and len(mutations(demo)) == mutation_count == 2
    )
    assert (
        recover_pending_intake(
            demo.config, task.id, original.id, approval, "expired-repair", **args
        )
        == result
    )
    assert read_private(demo.config) == config_before
    assert read_private(config_before["session_file"]) == capability_before
    assert history(service.decisions.journal, service.grant_ref) == old
    assert service.core.sync.list_operations(service.handle)[0].state == "uncertain"
    with pytest.raises(WorkError):
        prepare(demo.config, {"operation": "task_list", "data": {}})
    renewed = setup(demo.config, **renewal)
    assert renewed["enrollment_ref"] != service.grant_ref
    assert history(service.decisions.journal, service.grant_ref) == old
    assert read_private(config_before["session_file"]) == capability_before
    assert len(mutations(demo)) == mutation_count


@pytest.mark.parametrize(
    "fault", ["expired", "during-read", "changed-key", "drift", "unknown-send", "owner"]
)
def test_expired_read_only_recovery_refuses_without_provider_write(boot, monkeypatch, fault):  # noqa: F811
    demo, control = boot
    service, task, original, approval, now, _ = expired_repair(demo, monkeypatch)
    kwargs = dict(recovery_ref="explicit-recovery", deadline=now + 60)
    key = "expired-repair"
    if fault == "expired":
        kwargs["deadline"] = now
    elif fault == "changed-key":
        key = "another-repair"
    elif fault == "drift":
        issue = next(iter(demo.provider.issues.values()))
        demo.provider.edit(issue["id"], description="Unapproved external prose")
    elif fault == "unknown-send":
        with service.provider.journal.transaction() as conn:
            conn.execute("UPDATE sends SET snapshot=NULL WHERE operation_id!=?", (original.id,))
    elif fault == "owner":
        write_private(control["record"], {"owner": "present"})
    elif fault == "during-read":
        module = importlib.import_module("ai_task_manager.installation.workflows.bootstrap")
        original_load = module.load_service
        clock = [now]

        def timed(*args, **kwargs):
            current = original_load(*args, **kwargs)
            current.core.context.clock = lambda: clock[0]
            return current

        def expire(request, response):
            if "query ReadIssue" in request["query"]:
                clock[0] = now + 61

        monkeypatch.setattr(module, "load_service", timed)
        demo.provider.hook = expire
    count = len(mutations(demo))
    with pytest.raises((WorkError, LinearError)):
        recover_pending_intake(demo.config, task.id, original.id, approval, key, **kwargs)
    assert len(mutations(demo)) == count == 2
    assert service.core.get_task(service.handle, task.id).sync_state != "confirmed"
