"""Script-owned sequence and interruption controls over actual guarded local workers."""

from dataclasses import replace

import pytest

from ai_task_manager.application.workflows.reviews import ReviewedEvidence
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.workflows import WorkflowStore
from tests.workflows.helpers import WorkflowFixture


@pytest.fixture
def wf(tmp_path):
    value = WorkflowFixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_WD01_WD03_actual_script_omission_reaches_review_wait(wf):
    result = wf.driver.run(wf.ref)
    assert result["state"] == "awaiting_review", result
    assert result["reason"] == "semantic_review_unavailable"
    row = wf.store.read(wf.ref, wf.rig.scope)
    assert set(row["steps"]) == {"start", "execute", "verify"}
    assert all(v["result"] is not None for v in row["steps"].values())
    attempt = wf.rig.core.get_attempt(wf.rig.handle, row["attempt_id"])
    assert attempt.state == "done"
    assert wf.rig.current(wf.task).observed_status == "In Progress"
    assert (wf.runtime.pair(attempt.id).worktree / "result.txt").read_text().strip()
    assert wf.store.verified(wf.rig.scope, wf.task.id, result["artifact_digest"])
    assert wf.store.verified(wf.rig.scope, wf.task.id, "wrong") is None


def test_WD02_authored_opaque_review_completes_via_core(wf):
    driver = wf.new_driver(reviews=wf.authored_reviews())
    result = driver.run(wf.ref)
    assert result["state"] == "done", result
    task = wf.rig.current(wf.task)
    assert task.observed_status == "Done" and task.sync_state == "confirmed"
    assert task.completed_work == task.required_work
    assert driver.run(wf.ref)["state"] == "done"
    assert len(wf.runtime.dispatches) == 1


def test_WD06_resume_saved_result_does_not_execute_again(wf):
    first = wf.driver.run(wf.ref)
    before = wf.store.read(wf.ref, wf.rig.scope)
    wf.store = WorkflowStore(wf.store.path)
    assert wf.new_driver().run(wf.ref) == first
    assert wf.store.read(wf.ref, wf.rig.scope) == before
    assert len(wf.runtime.dispatches) == 1


class LostReply:
    def __init__(self, boundary):
        self.boundary = boundary
        self.fired = False

    def hit(self, boundary):
        if boundary == self.boundary and not self.fired:
            self.fired = True
            raise RuntimeError("authored lost reply")


@pytest.mark.parametrize("boundary", ["after:start", "after:execute", "after:verify"])
def test_WD07_WD08_reconcile_observed_effect_without_duplicate(wf, boundary):
    first = wf.new_driver(faults=LostReply(boundary)).run(wf.ref)
    assert first["state"] == "blocked"
    result = wf.new_driver().run(wf.ref)
    assert result["state"] == "awaiting_review", result
    assert len(wf.runtime.dispatches) == 1
    row = wf.store.read(wf.ref, wf.rig.scope)
    assert all(v["result"] is not None for v in row["steps"].values())


def test_WD08_unproved_effect_never_replays(wf):
    result = wf.new_driver(faults=LostReply("before:execute")).run(wf.ref)
    assert result["state"] == "blocked"
    attempt = wf.rig.core.get_attempt(
        wf.rig.handle, wf.store.read(wf.ref, wf.rig.scope)["attempt_id"]
    )
    assert not (wf.runtime.pair(attempt.id).worktree / "result.txt").exists()
    assert wf.new_driver().run(wf.ref)["state"] == "uncertain"
    assert not (wf.runtime.pair(attempt.id).worktree / "result.txt").exists()


def test_WD05_concurrent_claim_does_not_launch(wf):
    wf.store.claim(wf.ref, wf.rig.scope, "other-owner")
    with pytest.raises(AccessDenied):
        wf.driver.run(wf.ref)
    assert not wf.runtime.dispatches
    wf.store.release(wf.ref, "other-owner")


def test_WD04_foreign_scope_refuses(wf):
    with pytest.raises(AccessDenied):
        wf.store.read(wf.ref, replace(wf.rig.scope, logical_session="other-session"))
    assert not wf.runtime.dispatches


def test_WD04_wrong_file_cannot_deliver(tmp_path):
    wf = WorkflowFixture(
        tmp_path,
        program="from pathlib import Path;"
        "Path('result.txt').write_text('valid');Path('outside.txt').write_text('x')",
    )
    try:
        result = wf.driver.run(wf.ref)
        assert result["state"] == "blocked", result
        assert "trusted workflow scope" in result["detail"]
        assert not wf.rig.current(wf.task).completed_work
    finally:
        wf.close()


def test_WD09_human_hold_stops_before_execution(tmp_path):
    wf = WorkflowFixture(tmp_path, max_steps=1)
    try:
        assert wf.driver.run(wf.ref)["reason"] == "step_limit"
        dispatch = next(iter(wf.runtime.dispatches.values()))
        wf.rr.hold(dispatch)
        assert wf.driver.run(wf.ref)["reason"] == "human_or_assignment_change"
        assert not (dispatch.resources.worktree / "result.txt").exists()
        attempt = wf.rig.core.get_attempt(wf.rig.handle, dispatch.attempt_id)
        assert attempt.state == "paused"
    finally:
        wf.close()


def test_WD09_trusted_plan_revision_blocks(wf):
    assert (
        wf.changed_plan(allowed_files=("result.txt", "other.txt")).run(wf.ref)["state"] == "blocked"
    )
    assert not wf.runtime.dispatches


def test_WD10_missing_worker_port_blocks(wf):
    wf.driver.runtime = None
    assert wf.driver.run(wf.ref)["reason"] == "worker_or_repository_port_unavailable"
    assert not wf.runtime.dispatches


def test_WD11_model_passed_flag_is_not_review(wf):
    reviews = ReviewedEvidence(wf.rig.core, lambda *args: {"passed": True})
    result = wf.new_driver(reviews=reviews).run(wf.ref)
    assert result["state"] == "blocked"
    assert "passed flag" in result["detail"]
    assert not wf.rig.current(wf.task).completed_work


@pytest.mark.parametrize("field", ["task_id", "attempt_id"])
def test_WD12_lineage_refuses_cross_task_or_attempt(wf, field):
    assert wf.driver.run(wf.ref)["state"] == "awaiting_review"
    row = wf.store.read(wf.ref, wf.rig.scope)
    fact = wf.runtime.ownership(row["attempt_id"])
    values = {"task_id": wf.task.id, "attempt_id": row["attempt_id"]}
    values[field] = "foreign"
    with pytest.raises(AccessDenied):
        wf.store.lineage(values["task_id"], values["attempt_id"], fact)


def test_WD13_partial_artifact_revision_keeps_provenance(wf):
    assert wf.driver.run(wf.ref)["state"] == "awaiting_review"
    row = wf.store.read(wf.ref, wf.rig.scope)
    (wf.runtime.pair(row["attempt_id"]).worktree / "result.txt").write_text("amended")
    assert wf.driver.run(wf.ref)["reason"] == "artifact_revision"
    assert wf.store.read(wf.ref, wf.rig.scope)["steps"] == row["steps"]


def test_WD14_no_union_of_historical_recovery_refs(wf):
    import subprocess

    subprocess.run(
        ["git", "-C", str(wf.rr.repo), "update-ref", "refs/recovery/discarded-task", wf.plan.base],
        check=True,
    )
    result = wf.driver.run(wf.ref)
    assert result["state"] == "awaiting_review", result
    row = wf.store.read(wf.ref, wf.rig.scope)
    assert row["steps"]["verify"]["result"]["files"] == ["result.txt"]


def test_WD15_deadline_checkpoint_before_effect(wf):
    wf.rig.now = wf.plan.deadline
    assert wf.driver.run(wf.ref)["reason"] == "invocation_deadline"
    assert not wf.runtime.dispatches


def test_WD16_corrupt_persisted_observation_refuses(wf):
    assert wf.driver.run(wf.ref)["state"] == "awaiting_review"
    row = wf.store.read(wf.ref, wf.rig.scope)
    import json

    body = row["steps"]["verify"]["result"]
    body["files"] = ["invented.txt"]
    with wf.store.transaction() as conn:
        conn.execute("UPDATE steps SET result=? WHERE name='verify'", (json.dumps(body),))
    from ai_task_manager.domains.work.errors import SchemaError

    with pytest.raises(SchemaError):
        wf.store.verified(wf.rig.scope, wf.task.id, body["digest"])
    assert wf.driver.run(wf.ref)["reason"] == "SchemaError"


def test_WD09_changed_execution_binding_blocks(wf):
    from ai_task_manager.adapters.workflows.runtime import LocalCommands, LocalSupervisorPort

    changed = LocalCommands({"write-result": (("other-executable",), {}, 2)})
    driver = wf.new_driver()
    driver.runtime = LocalSupervisorPort(wf.rr.supervisor, changed)
    assert driver.run(wf.ref)["reason"] == "AccessDenied"
    assert not driver.runtime.dispatches


def test_WD11_wrong_artifact_review_refuses(wf):
    trusted = wf.authored_reviews()
    wrong = ReviewedEvidence(
        wf.rig.core, lambda scope, task, digest: trusted.lookup(scope, task, "different-artifact")
    )
    result = wf.new_driver(reviews=wrong).run(wf.ref)
    assert result["state"] == "blocked"
    assert "another artifact" in result["detail"]
    assert not wf.rig.current(wf.task).completed_work


def test_WD02_confirm_resume_does_not_require_new_review(wf):
    first = wf.new_driver(reviews=wf.authored_reviews(), faults=LostReply("after:deliver"))
    assert first.run(wf.ref)["state"] == "blocked"
    result = wf.new_driver().run(wf.ref)
    assert result["state"] == "done", result
    assert len(wf.runtime.dispatches) == 1


def test_WD12_persisted_foreign_attempt_refuses_before_runtime(wf):
    import json

    first = wf.rig.current(wf.task)
    wf.rig.core.execution.reserve(
        wf.rig.handle,
        first.id,
        "fixture",
        first.version,
        first.decision_revision,
        wf.rig.decision(first, "In Progress"),
        wf.rig.key(),
    )
    other = wf.rig.queued(code=False)
    attempt = wf.rig.core.execution.reserve(
        wf.rig.handle,
        other.id,
        "fixture",
        other.version,
        other.decision_revision,
        wf.rig.decision(other, "In Progress"),
        wf.rig.key(),
    )
    with wf.store.transaction() as conn:
        body = json.loads(conn.execute("SELECT body FROM runs").fetchone()[0])
        body["attempt_id"] = attempt.id
        conn.execute("UPDATE runs SET body=?", (json.dumps(body),))
    result = wf.driver.run(wf.ref)
    assert result["reason"] == "AccessDenied"
    assert "Canonical attempt" in result["detail"]
    assert not wf.runtime.dispatches


def test_WD16_inspection_wrong_prose_cannot_be_review_source(wf):
    import json

    from ai_task_manager.domains.checks.models import fingerprint
    from ai_task_manager.domains.work.errors import SchemaError

    result = wf.driver.run(wf.ref)
    assert result["state"] == "awaiting_review"
    row = wf.store.read(wf.ref, wf.rig.scope)
    body = row["steps"]["verify"]["result"]
    body["prose_digest"] = "different-scope"
    body["digest"] = fingerprint({k: v for k, v in body.items() if k != "digest"})
    with wf.store.transaction() as conn:
        conn.execute("UPDATE steps SET result=? WHERE name='verify'", (json.dumps(body),))
    with pytest.raises(SchemaError):
        wf.store.verified(wf.rig.scope, wf.task.id, body["digest"])
