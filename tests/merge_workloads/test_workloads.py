"""Finite actual default-port workload measurements with declared semantic review."""

import json
import time
from dataclasses import asdict

import pytest

from ai_task_manager.adapters.resources.git import git
from tests.merge_workloads.fixture import Owner, Project
from tests.merge_workloads.test_recovery import compose


def test_small_cold_and_same_task_warm_content_reuse(tmp_path):
    project = Project(tmp_path / "small")
    owner = Owner(project, 0)
    try:
        owner.candidate = owner.candidates.prepare(owner.grant)
        assert owner.candidate.status == "ready", owner.candidate
        inputs = owner.capability.checks
        from ai_task_manager.domains.checks.models import Scope

        scope = Scope(
            owner.r.scope.principal,
            owner.r.scope.project,
            owner.r.scope.logical_session,
            owner.task.id,
        )
        before = time.monotonic()
        cold = inputs.prepare(owner.candidates.profile, owner.grant, owner.candidate, "cold")
        first = inputs.checks.run(cold, scope)
        cold_seconds = time.monotonic() - before
        assert first["outcome"] == "passed", first
        jobs = len(inputs.checks.source_guard.runner.jobs)
        before = time.monotonic()
        warm = inputs.prepare(owner.candidates.profile, owner.grant, owner.candidate, "warm")
        second = inputs.checks.run(warm, scope)
        warm_seconds = time.monotonic() - before
        assert second["outcome"] == "passed", second
        assert len(inputs.checks.source_guard.runner.jobs) == jobs == 1
        assert len(first["plan"].units) == len(second["plan"].units) == 1
        owner.workflow = compose(owner)
        result = owner.workflow.run(owner.task.id, owner.workflow.plan.id, "small-delivery")
        assert result["data"]["state"] == "done", result
        assert git(project.repo, "show", "integration:features/0.txt") == owner.value.strip()
        (tmp_path / "small-timings.json").write_text(
            json.dumps(
                {
                    "database_seconds": owner.database_seconds,
                    "worker_commit_seconds": owner.worker_commit_seconds,
                    "setup_seconds": owner.setup_seconds,
                    "cold_check_seconds": cold_seconds,
                    "warm_check_seconds": warm_seconds,
                    "docker_jobs": jobs,
                    "workflow_stages": owner.workflow.timings,
                    "cold_plan": asdict(first["plan"]),
                    "warm_groups": second["groups"],
                },
                indent=2,
            )
        )
    finally:
        owner.close()


def test_broad_eight_line_config_selects_all_four_checks(tmp_path):
    project = Project(tmp_path / "broad")
    owner = Owner(project, 0, broad=True)
    started = time.monotonic()
    try:
        owner.workflow = compose(owner)
        result = owner.workflow.run(owner.task.id, owner.workflow.plan.id, "broad-delivery")
        assert result["data"]["state"] == "done", result
        assert git(project.repo, "show", "integration:settings.json") == owner.value.strip()
        row = owner.workflow.store.read(result["data"]["workflow"])
        from ai_task_manager.domains.checks.models import Scope

        scope = Scope(
            owner.r.scope.principal,
            owner.r.scope.project,
            owner.r.scope.logical_session,
            owner.task.id,
        )
        checks = owner.capability.checks.checks.store.read(
            row["steps"]["checks"]["result"]["ref"], scope
        )
        assert len(checks["plan"].units) == 4
        assert len(owner.capability.checks.checks.source_guard.runner.jobs) == 4
        timings = {
            "database_seconds": owner.database_seconds,
            "worker_commit_seconds": owner.worker_commit_seconds,
            "setup_seconds": owner.setup_seconds,
            "delivery_seconds": time.monotonic() - started,
            "stages": owner.workflow.timings,
            "selected_units": [u.name for u in checks["plan"].units],
        }
        (tmp_path / "broad-timings.json").write_text(json.dumps(timings, indent=2))
        assert timings["setup_seconds"] + timings["delivery_seconds"] <= 240
    finally:
        owner.close()


@pytest.mark.parametrize("count", [1, 2, 4])
def test_successful_ordered_contenders_keep_every_owned_eight_line_change(tmp_path, count):
    import concurrent.futures
    import threading

    project = Project(tmp_path / f"contenders-{count}")
    owners = []
    started = time.monotonic()
    barrier = threading.Barrier(count)
    completed = []
    errors = []
    lock = threading.Lock()
    try:
        for i in range(count):
            owners.append(Owner(project, i))

        def deliver(owner):
            owner.workflow = compose(owner)
            barrier.wait(timeout=10)
            began = time.monotonic()
            try:
                result = owner.workflow.run(
                    owner.task.id, owner.workflow.plan.id, "ordered-delivery"
                )
                with lock:
                    completed.append(
                        {
                            "number": owner.number,
                            "task_id": owner.task.id,
                            "attempt_id": owner.attempt.id,
                            "branch": owner.grant.source_ref,
                            "database": str(owner.pair.database),
                            "result": result,
                            "seconds": time.monotonic() - began,
                            "stages": owner.workflow.timings,
                        }
                    )
                assert result["data"]["state"] == "done", result
            except Exception as error:
                with lock:
                    errors.append(
                        {
                            "number": owner.number,
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                    )
                raise

        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(deliver, owner) for owner in owners]
            for future in futures:
                future.result(timeout=600)
        assert len(completed) == count
        for owner in owners:
            assert (
                git(project.repo, "show", f"integration:features/{owner.number}.txt")
                == owner.value.strip()
            )
            assert git(owner.pair.worktree, "rev-parse", "HEAD") == owner.grant.source_head
            assert len(owner.runtime.runtime.workers) == 1
        assert len({owner.task.id for owner in owners}) == count
        assert time.monotonic() - started <= 600
    finally:
        (tmp_path / f"contenders-{count}-timings.json").write_text(
            json.dumps(
                {
                    "count": count,
                    "seconds": time.monotonic() - started,
                    "completed": completed,
                    "errors": errors,
                },
                indent=2,
            )
        )
        for owner in owners:
            owner.close()


def test_last_consumer_wait_reacquires_owned_database_and_invalidates_old_receipt(tmp_path):
    import sqlite3

    from ai_task_manager.domains.checks.models import Scope
    from ai_task_manager.domains.work.errors import AccessDenied
    from ai_task_manager.supervisor.models import RuntimeRefused

    project = Project(tmp_path / "recovery")
    owner = Owner(project, 0)
    try:
        pair = owner.pair
        inode = pair.database.stat().st_ino
        owner.candidate = owner.candidates.prepare(owner.grant)
        inputs = owner.capability.checks
        scope = Scope(
            owner.r.scope.principal,
            owner.r.scope.project,
            owner.r.scope.logical_session,
            owner.task.id,
        )
        cold = inputs.prepare(owner.candidates.profile, owner.grant, owner.candidate, "before-wait")
        assert inputs.checks.run(cold, scope)["outcome"] == "passed"
        with owner.runtime.resources.use(pair):
            with pytest.raises(RuntimeRefused, match="consumer"):
                owner.runtime.resources.cleanup(owner.r.core, owner.r.handle, pair.attempt_id)
        with pytest.raises(RuntimeRefused, match="delivery"):
            owner.runtime.resources.cleanup(owner.r.core, owner.r.handle, pair.attempt_id)
        started = time.monotonic()
        with owner.runtime.resources.use(pair):
            owner.runtime.resources.verify(pair)
            assert pair.database.stat().st_ino == inode
            with sqlite3.connect(pair.database) as connection:
                assert connection.execute("SELECT attempt_id FROM fixture_owner").fetchall() == [
                    (pair.attempt_id,)
                ]
                connection.execute("CREATE TABLE application_input(value INTEGER)")
                connection.execute("INSERT INTO application_input VALUES(1)")
            before = len(inputs.checks.source_guard.runner.jobs)
            with pytest.raises(AccessDenied):
                inputs.checks.run(cold, scope)
            assert len(inputs.checks.source_guard.runner.jobs) == before
            fresh = inputs.prepare(
                owner.candidates.profile, owner.grant, owner.candidate, "after-wait"
            )
            assert inputs.checks.run(fresh, scope)["outcome"] == "passed"
            assert len(inputs.checks.source_guard.runner.jobs) == before + 1
            owner.workflow = compose(owner)
            result = owner.workflow.run(owner.task.id, owner.workflow.plan.id, "resumed-consumer")
            assert result["data"]["state"] == "done", result
        cleanup = owner.runtime.resources.cleanup(owner.r.core, owner.r.handle, pair.attempt_id)
        assert cleanup == {"worktree": "removed", "database": "removed"}
        assert not pair.database.exists() and not pair.worktree.exists()
        (tmp_path / "last-consumer-result.json").write_text(
            json.dumps(
                {
                    "reacquisition_and_delivery_seconds": time.monotonic() - started,
                    "original_database_inode": inode,
                    "old_receipt_refused": True,
                    "fresh_physical_jobs": before + 1,
                    "cleanup": cleanup,
                },
                indent=2,
            )
        )
    finally:
        owner.close()
