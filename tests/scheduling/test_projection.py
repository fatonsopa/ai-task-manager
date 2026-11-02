import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.application.service.projection import attempt_view, task_view
from ai_task_manager.application.service.queries import task_list
from ai_task_manager.domains.scheduling.models import Dependency
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import IssueStatus as S

from .helpers import declare


@pytest.mark.parametrize("variant", ["other_session", "other_principal", "external_dependency", "quarantined_task", "mixed_task_snapshot", "mixed_attempt_snapshot"])
def test_session_scope_and_foreign_prose(variant, rig, monkeypatch):
    service = SimpleNamespace(core=rig.core, handle=rig.handle)
    task = rig.task()
    if variant.startswith("mixed_"):
        task, attempt = rig.running(rig.transition(task, S.TODO, "enqueue"))
        original = rig.core.scheduling.explain
        def mixed(handle, task_id):
            value = original(handle, task_id)
            return {**value, "task_version": value["task_version"] + 1}
        monkeypatch.setattr(rig.core.scheduling, "explain", mixed)
        view = task_view(service, task) if variant == "mixed_task_snapshot" else attempt_view(service, attempt)
        assert view["work_authorized"] is False
        assert view["scheduling"]["state"] == "unknown"
        return
    if variant in {"other_session", "other_principal"}:
        scope = replace(rig.scope, logical_session="foreign-session",
                        principal="foreign-principal" if variant == "other_principal" else rig.scope.principal)
        handle = rig.session(scope)
        foreign = rig.task(handle=handle, confirmed=False)
        with pytest.raises(AccessDenied):
            task_view(service, foreign)
        assert [item["id"] for item in task_list(service, {}, None)["data"]] == [task.id]
    elif variant == "external_dependency":
        declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now,
                (Dependency("external:private-issue", "work_done"),))
        view = task_view(service, task)
        assert view["scheduling"]["state"] == "blocked"
        assert "external:private-issue" not in json.dumps(view)
        assert view["scheduling"]["blockers"][0]["dependency"] is None
    else:
        proof = rig.proof("external_observation", task.id, {
            "issue_id": task.issue_id, "source_revision": "2026-11-02T00:00:00Z",
            "status": "Backlog", "assigned_to_owner": False,
        })
        rig.core.external.observe(rig.handle, task.id, task.version, 0, proof, rig.key())
        view = task_view(service, rig.current(task))
        assert view["quarantined"] and view["body"] is None
        assert not task_list(service, {}, None)["data"]


@pytest.mark.parametrize("variant", ["todo", "in_progress", "on_hold", "unknown_attempt"])
def test_blocked_activity_preserves_status(variant, rig):
    attempt = None
    if variant in {"in_progress", "on_hold", "unknown_attempt"}:
        task, attempt = rig.running()
        if variant == "unknown_attempt":
            task, attempt = rig.settle(task, attempt, "unknown", quiescent=False)
        elif variant == "on_hold":
            task, attempt = rig.settle(task, attempt)
            task = rig.transition(task, S.ON_HOLD, "hold")
    else:
        task = rig.queued()
    active_before = None
    if attempt:
        with rig.db.transaction() as conn:
            attempt_before = tuple(conn.execute("SELECT * FROM attempts WHERE id=?", (attempt.id,)).fetchone())
            resources_before = [tuple(row) for row in conn.execute(
                "SELECT * FROM resource_leases WHERE attempt_id=? ORDER BY kind,resource_id", (attempt.id,))]
            active_before = conn.execute("SELECT active FROM attempts WHERE id=?", (attempt.id,)).fetchone()[0]
        assert active_before == int(variant != "on_hold")
    status = task.observed_status
    declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now,
            (Dependency("missing", "work_done"),))
    service = SimpleNamespace(core=rig.core, handle=rig.handle)
    view = task_view(service, task)
    assert view["observed_status"] == status
    assert view["activity"] == "blocked" and view["work_authorized"] is False
    assert rig.current(task).observed_status == status
    if attempt:
        assert attempt_view(service, attempt)["work_authorized"] is False
        with rig.db.transaction() as conn:
            assert conn.execute("SELECT active FROM attempts WHERE id=?", (attempt.id,)).fetchone()[0] == active_before
            assert tuple(conn.execute("SELECT * FROM attempts WHERE id=?", (attempt.id,)).fetchone()) == attempt_before
            assert [tuple(row) for row in conn.execute(
                "SELECT * FROM resource_leases WHERE attempt_id=? ORDER BY kind,resource_id", (attempt.id,))] == resources_before
