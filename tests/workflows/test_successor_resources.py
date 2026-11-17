"""Actual local Git/database/child continuation with authored task authority."""

import os
import secrets
import sqlite3
import sys
from contextlib import nullcontext
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
from ai_task_manager.adapters.workflows.repository import RepositoryInspector
from ai_task_manager.adapters.workflows.runtime import LocalCommands, LocalSupervisorPort
from ai_task_manager.application.workflows.driver import WorkflowDriver
from ai_task_manager.application.workflows.models import WorkflowPlan, WorkflowPlans
from ai_task_manager.application.workflows.recovery import SuccessorAuthorization, admit_successor
from ai_task_manager.domains.work.models import Evidence, SessionHandle
from ai_task_manager.storage.workflows import WorkflowStore
from tests.runtime.helpers import ProviderFixture, RuntimeRig


@pytest.mark.parametrize("code", [False, True])
def test_stopped_active_successor_uses_original_dirty_files_and_database(tmp_path, code):
    rr = RuntimeRig(tmp_path)
    r = rr.rig
    try:
        task = r.queued(code=code, units=("first", "remaining"))
        original = rr.start(task)
        rr.admit(original)
        task = r.core.get_task(r.handle, task.id)
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {"provider_status", "decision", "human_decision", "work_unit_complete"}
                    ),
                    r.proofs,
                ),
            ]
        )
        r.core.execution.complete_unit(
            r.handle,
            task.id,
            "first",
            task.version,
            task.decision_revision,
            r.proof(
                "work_unit_complete", task.id, {"prose_digest": task.prose_digest, "unit": "first"}
            ),
            r.key(),
        )
        pair = original.resources
        (pair.worktree / "partial.txt").write_text("retain this partial work\n")
        with sqlite3.connect(pair.database) as db:
            db.execute("CREATE TABLE partial(value TEXT)")
            db.execute("INSERT INTO partial VALUES('retained')")
        rr.supervisor.stop(r.handle, original, operation_key=r.key())
        task = r.core.get_task(r.handle, task.id)
        assert task.execution_state == "paused"
        base = rr.resources.git.inspect(pair.worktree, "managed/" + original.attempt_id)["head"]
        old = WorkflowPlan(
            "old",
            "continue",
            "fixture",
            base,
            ("partial.txt", "result.txt"),
            ("result.txt",),
            r.now - 1,
        )
        fresh = replace(old, id="fresh", deadline=r.now + 100, max_steps=2)
        store = WorkflowStore(tmp_path / "workflow.sqlite")
        predecessor = store.create(r.scope, task, old, "original-key", "old-runtime")
        store.claim(predecessor, r.scope, "old-owner")
        store.intend(predecessor, "old-owner", "start", {"original": True})
        store.finish(
            predecessor,
            "old-owner",
            "start",
            {"attempt_id": original.attempt_id},
            attempt_id=original.attempt_id,
        )
        store.release(predecessor, "old-owner")
        old_row = store.read(predecessor, r.scope)
        decision = r.proof(
            "decision",
            task.id,
            {"target": "In Progress", "revision": 0, "actor": "agent"},
            expires_at=r.now + 120,
        )
        a = SuccessorAuthorization(
            r.key(),
            r.scope,
            predecessor,
            task.id,
            task.version,
            0,
            fresh.id,
            fresh.digest,
            "fresh-key",
            r.now + 100,
            decision,
            ("remaining",),
        )
        r.proofs.evidence[a.reference] = Evidence(
            "workflow_successor", r.scope, task.id, asdict(a), a.expires_at
        )
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {
                            "provider_status",
                            "decision",
                            "human_decision",
                            "workflow_successor",
                            "work_unit_complete",
                        }
                    ),
                    r.proofs,
                ),
            ]
        )
        handle = SessionHandle(secrets.token_urlsafe(32))
        r.core.revoke_session(r.handle)
        commands = LocalCommands(
            {
                "continue": (
                    (
                        sys.executable,
                        "-I",
                        "-c",
                        "from pathlib import Path;"
                        "Path('result.txt').write_text(Path('partial.txt').read_text())",
                    ),
                    {"PATH": os.defpath},
                    2,
                )
            }
        )
        runtime = LocalSupervisorPort(rr.supervisor, commands)
        boundary = SimpleNamespace(
            check=lambda *_: None, check_locked=lambda *_: None, boundary=lambda *_: nullcontext()
        )
        ref = admit_successor(
            r.core, r.handle, handle, store, a, fresh, runtime.binding(task, fresh), boundary
        )
        rr.resources.successor_sources[task.id] = original.attempt_id
        driver = WorkflowDriver(
            r.core,
            handle,
            store,
            WorkflowPlans((fresh,)),
            runtime=runtime,
            repository=RepositoryInspector(runtime),
            provider=ProviderFixture(r),
            decisions=lambda scope, task, target, action: r.decision(task, target),
        )
        result = driver.run(ref)
        assert result["state"] == "checkpoint", result
        current = store.read(ref, r.scope)
        new_attempt = r.core.get_attempt(handle, current["attempt_id"])
        assert new_attempt.id != original.attempt_id and new_attempt.state == "done"
        inherited = runtime.pair(new_attempt.id)
        assert inherited.worktree == pair.worktree and inherited.database == pair.database
        assert (inherited.worktree / "result.txt").read_text() == "retain this partial work\n"
        with sqlite3.connect(pair.database) as db:
            assert db.execute("SELECT value FROM partial").fetchall() == [("retained",)]
            assert db.execute("SELECT attempt_id FROM fixture_owner").fetchall() == [
                (original.attempt_id,)
            ]
        assert r.core.get_task(handle, task.id).completed_work == ("first",)
        if code:
            # R06: the installed delivery consumer sees the new attempt's full
            # own lifecycle, rather than only the final author checkpoint.
            from tests.scheduling.helpers import known_declaration

            current = r.core.get_task(handle, task.id)
            current = r.core.execution.complete_unit(
                handle,
                task.id,
                "remaining",
                current.version,
                current.decision_revision,
                r.proof(
                    "work_unit_complete",
                    task.id,
                    {"unit": "remaining", "prose_digest": current.prose_digest},
                ),
                r.key(),
            )
            known_declaration(r, current, handle)
            r.core.scheduling.successor_delivery(handle, task.id, new_attempt.id)
            with r.core.execution.worker_commit_boundary(
                handle,
                task.id,
                new_attempt.id,
                current.version,
                new_attempt.version,
                current.decision_revision,
            ):
                pass
            assert len(rr.runtime.workers) == 2

        assert store.read(predecessor, r.scope) == old_row
    finally:
        rr.close()


def test_R04_own_remaining_unit_progress_binds_current_delivery_version(tmp_path):
    from tests.scheduling.helpers import known_declaration

    rr = RuntimeRig(tmp_path)
    r = rr.rig
    try:
        task = r.queued(code=True, units=("first", "remaining"))
        dispatch = rr.start(task)
        rr.admit(dispatch)
        rr.supervisor.execute(r.handle, dispatch, rr.command(dispatch), operation_key=r.key())
        r.core.context.evidence = EvidenceRouter(
            [
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
                EvidenceRoute(
                    "fixture:",
                    frozenset(
                        {
                            "provider_status",
                            "decision",
                            "human_decision",
                            "work_unit_complete",
                            "workflow_successor",
                        }
                    ),
                    r.proofs,
                ),
            ]
        )

        def complete(handle, unit):
            current = r.core.get_task(handle, task.id)
            return r.core.execution.complete_unit(
                handle,
                current.id,
                unit,
                current.version,
                current.decision_revision,
                r.proof(
                    "work_unit_complete",
                    current.id,
                    {"unit": unit, "prose_digest": current.prose_digest},
                ),
                r.key(),
            )

        first = complete(r.handle, "first")
        r.now += 200
        decision = r.proof(
            "decision",
            task.id,
            {"target": "In Progress", "revision": 0, "actor": "agent"},
            expires_at=r.now + 150,
        )
        a = SuccessorAuthorization(
            r.key(),
            r.scope,
            "workflow:old",
            task.id,
            first.version,
            0,
            "fresh",
            "digest",
            "fresh-key",
            r.now + 120,
            decision,
            ("remaining",),
        )
        r.proofs.evidence[a.reference] = Evidence(
            "workflow_successor", r.scope, task.id, asdict(a), a.expires_at
        )
        handle = SessionHandle(secrets.token_urlsafe(32))
        r.core.revoke_session(r.handle)
        r.core.admit_workflow_successor(r.handle, handle, a, lambda *_: "workflow:new")
        current = complete(handle, "remaining")
        assert current.version == a.task_version + 1
        known_declaration(r, current, handle)
        checkpoint = r.core.scheduling.successor_delivery(handle, task.id, dispatch.attempt_id)
        attempt = r.core.get_attempt(handle, dispatch.attempt_id)
        assert checkpoint["task_version"] == current.version
        with r.core.execution.worker_commit_boundary(
            handle, task.id, attempt.id, current.version, attempt.version, current.decision_revision
        ):
            pass
        assert len(rr.runtime.workers) == 1
        # An unrelated version advance has no authorized own-unit proof.
        from ai_task_manager.storage import work

        with r.db.transaction() as conn:
            changed = work.save(conn, replace(current, title="Foreign revision"), current.version)
        from ai_task_manager.domains.work.errors import EvidenceRequired, WorkError

        with pytest.raises(EvidenceRequired, match="Foreign task progress"):
            r.core.scheduling.successor_delivery(handle, changed.id, attempt.id)
        with pytest.raises(WorkError):
            with r.core.execution.worker_commit_boundary(
                handle,
                task.id,
                attempt.id,
                changed.version,
                attempt.version,
                changed.decision_revision,
            ):
                pass
    finally:
        rr.close()
