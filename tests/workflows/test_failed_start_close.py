"""Failed-start recovery over real isolated Core, resources and waiting workers."""

import json
from dataclasses import replace

import pytest

from ai_task_manager.application.workflows.service import WorkflowService
from ai_task_manager.domains.work.errors import AccessDenied
from tests.workflows.helpers import WorkflowFixture


@pytest.fixture
def failed(tmp_path, monkeypatch):
    wf = WorkflowFixture(tmp_path)
    admit = wf.rr.supervisor.admit

    def fail_after_admission(handle, dispatch, *, operation_key):
        admit(handle, dispatch, operation_key=operation_key)
        # Reproduce the managed-start catch after Core grants the work lease,
        # before the adapter returns an attempt to the workflow controller.
        wf.rr.supervisor._abort(
            handle,
            dispatch.attempt_id,
            dispatch.worker,
            "AuthoredAdmissionFailure",
            wf.ref + ":start",
        )
        raise AccessDenied("Authored admission failure before start returns")

    monkeypatch.setattr(wf.rr.supervisor, "admit", fail_after_admission)
    service = WorkflowService(wf.driver)
    try:
        result = service.resume(wf.ref)["data"]
        assert result["state"] == "blocked" and result["reason"] == "AccessDenied"
        row = wf.store.read(wf.ref, wf.rig.scope)
        assert row["attempt_id"] is None and row["steps"]["start"]["result"] is None
        events = wf.rig.core.notifications.list_events(wf.rig.handle)
        reservations = [e for e in events if e.kind == "attempt_reserved"]
        assert len(reservations) == 1
        attempt = wf.rig.core.get_attempt(wf.rig.handle, reservations[0].payload["attempt_id"])
        assert attempt.state == "failed"
        assert wf.rig.current(wf.task).execution_state == "failed"
        assert len(wf.rr.runtime.workers) == 1
        worker = wf.rr.runtime.workers[attempt.id]
        assert worker.process.poll() is not None
        assert wf.rr.journal.observations(attempt.id, "runtime_outcome")[-1]["quiescent"] is True
        yield wf, service, attempt
    finally:
        wf.close()


def snapshot(wf):
    return (
        wf.rig.core.notifications.list_events(wf.rig.handle),
        len(wf.rr.runtime.workers),
        {key: value.process.poll() for key, value in wf.rr.runtime.workers.items()},
        wf.rig.current(wf.task),
    )


def test_failed_start_close_records_failure_once_without_another_effect(failed, monkeypatch):
    wf, service, attempt = failed
    before = snapshot(wf)
    monkeypatch.setattr(wf.runtime, "start", lambda *a: pytest.fail("No second launch"))
    monkeypatch.setattr(wf.runtime, "stop", lambda *a: pytest.fail("Failure is already settled"))
    first = service.close()
    assert len(first) == 1 and first[0]["state"] == "failed", first
    assert first[0]["failed_start"] is True and first[0]["quiescent"] is True
    assert first[0]["attempt_id"] == attempt.id
    row = wf.store.read(wf.ref, wf.rig.scope)
    assert row["attempt_id"] == attempt.id
    assert row["steps"]["start"]["result"]["state"] == "failed"
    assert set(row["steps"]) == {"start"}
    assert service.close() == first
    assert snapshot(wf) == before
    assert wf.new_driver().run(wf.ref)["reason"] == "attempt_failed"
    assert wf.rig.current(wf.task).observed_status != "Done"
    assert not (wf.runtime.pair(attempt.id).worktree / "result.txt").exists()


def test_resume_reconciles_failed_start_without_claiming_success(failed):
    wf, _, attempt = failed
    result = wf.new_driver().run(wf.ref)
    assert result["state"] == "blocked" and result["reason"] == "attempt_failed"
    row = wf.store.read(wf.ref, wf.rig.scope)
    assert row["attempt_id"] == attempt.id and row["steps"]["start"]["result"]["failed_start"]
    assert len(wf.rr.runtime.workers) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_operation",
        "wrong_decision",
        "wrong_cursor",
        "wrong_session",
        "wrong_task",
        "multiple_candidates",
        "missing_outcome",
        "unknown",
        "stale_proof",
        "changed_source",
        "not_quiescent",
        "wrong_worker",
        "wrong_host",
        "wrong_proof_task",
    ],
)
def test_failed_start_requires_exact_canonical_failure(failed, monkeypatch, request, fault):
    wf, service, attempt = failed
    row = wf.store.read(wf.ref, wf.rig.scope)
    if fault in {"wrong_decision", "wrong_cursor"}:
        payload = dict(row["steps"]["start"]["payload"])
        payload["decision_ref" if fault == "wrong_decision" else "cursor"] = (
            "fixture:foreign-decision" if fault == "wrong_decision" else 0
        )
        with wf.store.transaction() as conn:
            conn.execute(
                "UPDATE steps SET payload=? WHERE run_id=? AND name='start'",
                (json.dumps(payload), wf.ref),
            )
    elif fault == "wrong_operation":
        with wf.rig.db.transaction() as conn:
            conn.execute("DELETE FROM operations WHERE key=?", (wf.ref + ":start:reserve",))
    elif fault == "wrong_session":
        wf.driver.session_id = "foreign-session"
    elif fault == "wrong_task":
        other = wf.rig.queued(code=False)
        with wf.store.transaction() as conn:
            body = json.loads(
                conn.execute("SELECT body FROM runs WHERE id=?", (wf.ref,)).fetchone()[0]
            )
            body["task_id"] = other.id
            conn.execute("UPDATE runs SET body=? WHERE id=?", (json.dumps(body), wf.ref))
    elif fault == "multiple_candidates":
        current = wf.rig.current(wf.task)
        wf.rig.core.execution.reserve(
            wf.rig.handle,
            current.id,
            "fixture",
            current.version,
            current.decision_revision,
            wf.rig.decision(current, "In Progress"),
            "different-reservation",
        )
    elif fault == "missing_outcome":
        with wf.rig.db.transaction() as conn:
            conn.execute(
                "UPDATE events SET kind='unavailable_outcome' WHERE kind='attempt_outcome_recorded'"
            )
    elif fault == "unknown":
        from ai_task_manager.storage import attempts

        with wf.rig.db.transaction() as conn:
            attempts.save(conn, replace(attempt, state="unknown", version=attempt.version + 1))
    elif fault == "stale_proof":
        wf.rig.now += 61
    elif fault == "changed_source":
        with wf.rr.journal.transaction() as conn:
            original = conn.execute(
                "SELECT id,data FROM runtime_observations WHERE kind='runtime_outcome'"
            ).fetchall()
            conn.execute("UPDATE runtime_observations SET data='{}' WHERE kind='runtime_outcome'")

        def restore_source():
            with wf.rr.journal.transaction() as conn:
                conn.executemany(
                    "UPDATE runtime_observations SET data=? WHERE id=?",
                    [(value, identity) for identity, value in original],
                )

        request.addfinalizer(restore_source)
    else:
        verify = wf.rr.proofs.verify_evidence

        def altered(ref):
            evidence = verify(ref)
            field, value = {
                "not_quiescent": ("quiescent", False),
                "wrong_worker": ("worker_id", "foreign"),
                "wrong_host": ("host", "foreign"),
                "wrong_proof_task": ("task_id", "foreign"),
            }[fault]
            return replace(evidence, payload={**evidence.payload, field: value})

        monkeypatch.setattr(wf.rr.proofs, "verify_evidence", altered)
    before = snapshot(wf)
    closed = service.close()
    assert len(closed) == 1 and closed[0]["state"] == "uncertain", closed
    after = wf.store.read(wf.ref, wf.rig.scope)
    assert after["attempt_id"] is None and after["steps"]["start"]["result"] is None
    assert snapshot(wf) == before
