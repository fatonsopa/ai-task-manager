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
        owner.candidate = owner.candidates.prepare(owner.grant)
        inputs = owner.capability.checks
        from ai_task_manager.domains.checks.models import Scope

        scope = Scope(
            owner.r.scope.principal,
            owner.r.scope.project,
            owner.r.scope.logical_session,
            owner.task.id,
        )
        before = time.monotonic()
        cold_ref = inputs.prepare(
            owner.candidates.profile, owner.grant, owner.candidate, "broad-cold"
        )
        cold = inputs.checks.run(cold_ref, scope)
        cold_seconds = time.monotonic() - before
        assert cold["outcome"] == "passed"
        before = time.monotonic()
        warm_ref = inputs.prepare(
            owner.candidates.profile, owner.grant, owner.candidate, "broad-warm"
        )
        warm = inputs.checks.run(warm_ref, scope)
        warm_seconds = time.monotonic() - before
        assert warm["outcome"] == "passed"
        assert len(inputs.checks.source_guard.runner.jobs) == 4
        assert sum(group["result"]["carried_count"] for group in warm["groups"].values()) == 4
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
            "cold_check_seconds": cold_seconds,
            "warm_check_seconds": warm_seconds,
            "warm_carried_units": 4,
            "docker_jobs": 4,
        }
        (tmp_path / "broad-timings.json").write_text(json.dumps(timings, indent=2))
        assert timings["setup_seconds"] + timings["delivery_seconds"] <= 240
    finally:
        owner.close()


@pytest.mark.parametrize("count", [1, 2, 4])
def test_successful_ordered_contenders_keep_every_owned_eight_line_change(
    tmp_path, count, resume_waits=False
):
    import concurrent.futures
    import threading

    project = Project(tmp_path / f"contenders-{count}")
    owners = []
    started = time.monotonic()
    barrier = threading.Barrier(count)
    completed = []
    errors = []
    lock = threading.Lock()
    stop_monitor = threading.Event()
    peaks = {
        "merge_controllers": 0,
        "docker_watchers": 0,
        "candidate_git_children": 0,
        "publication_children": 0,
    }
    active_controllers = 0
    observer_errors = []

    def observe():
        try:
            while not stop_monitor.is_set():
                docker = candidates = publication = 0
                for owner in owners:
                    jobs = list(owner.capability.checks.checks.source_guard.runner.jobs.values())
                    docker += sum(not getattr(job.get("stop"), "confirmed", False) for job in jobs)
                    candidates += len(owner.candidates.runner.live)
                    if owner.workflow:
                        publication += sum(
                            sum(
                                len(children)
                                for children in list(value.service.publisher._live.values())
                            )
                            for value in list(owner.workflow.consumers.values())
                        )
                with lock:
                    peaks["docker_watchers"] = max(peaks["docker_watchers"], docker)
                    peaks["candidate_git_children"] = max(
                        peaks["candidate_git_children"], candidates
                    )
                    peaks["publication_children"] = max(peaks["publication_children"], publication)
                stop_monitor.wait(0.02)
        except BaseException as error:
            with lock:
                observer_errors.append({"type": type(error).__name__, "message": str(error)})
            stop_monitor.set()

    monitor = None
    try:
        for i in range(count):
            owners.append(Owner(project, i))

        monitor = threading.Thread(target=observe, name="owned-workload-observer")
        monitor.start()

        def deliver(owner):
            nonlocal active_controllers
            owner.workflow = compose(owner)
            barrier.wait(timeout=10)
            began = time.monotonic()
            with lock:
                active_controllers += 1
                peaks["merge_controllers"] = max(peaks["merge_controllers"], active_controllers)
            try:
                from ai_task_manager.domains.work.errors import AccessDenied

                calls = []
                for call in range(2 if resume_waits else 1):
                    call_started = time.monotonic()
                    try:
                        result = owner.workflow.run(
                            owner.task.id, owner.workflow.original_plan.id, "ordered-delivery"
                        )
                        calls.append(
                            {
                                "call": call + 1,
                                "seconds": time.monotonic() - call_started,
                                "state": result["data"]["state"],
                            }
                        )
                        break
                    except AccessDenied as error:
                        if (
                            not resume_waits
                            or str(error) != "Original repository turn wait budget expired"
                            or call
                        ):
                            raise
                        calls.append(
                            {
                                "call": call + 1,
                                "seconds": time.monotonic() - call_started,
                                "state": "bounded_no_effect_wait_refusal",
                                "reason": str(error),
                            }
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
                            "prepared_before_call_seconds": began - owner.queued_at,
                            "database_seconds": owner.database_seconds,
                            "worker_commit_seconds": owner.worker_commit_seconds,
                            "setup_seconds": owner.setup_seconds,
                            "stages": owner.workflow.timings,
                            "public_calls": calls,
                            "cumulative_repository_wait_seconds": (
                                sum(
                                    c["seconds"]
                                    for c in calls
                                    if c["state"] == "bounded_no_effect_wait_refusal"
                                )
                                + sum(
                                    stage["seconds"]
                                    for stage in owner.workflow.timings
                                    if stage["stage"] == "repository_wait"
                                )
                            ),
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
            finally:
                with lock:
                    active_controllers -= 1

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
            git(project.repo, "merge-base", "--is-ancestor", owner.grant.source_head, "integration")
        assert peaks["merge_controllers"] == count
        assert not observer_errors, observer_errors
        assert 1 <= peaks["docker_watchers"] <= 4
        assert 1 <= peaks["publication_children"] <= 1
        assert peaks["candidate_git_children"] >= 1
        assert len({owner.pair.database.stat().st_ino for owner in owners}) == count
        assert len({owner.task.id for owner in owners}) == count
        assert time.monotonic() - started <= 600
        assert all(row["cumulative_repository_wait_seconds"] <= 120 for row in completed), completed
    finally:
        stop_monitor.set()
        if monitor is not None:
            monitor.join(2)
        (tmp_path / f"contenders-{count}-timings.json").write_text(
            json.dumps(
                {
                    "count": count,
                    "seconds": time.monotonic() - started,
                    "completed": completed,
                    "errors": errors,
                    "observed_peaks": peaks,
                    "observer_errors": observer_errors,
                    "sampling_seconds": 0.02,
                    "same_key_wait_resume_allowed": resume_waits,
                    "frozen_queue_budget_met": all(
                        row["cumulative_repository_wait_seconds"] <= 120 for row in completed
                    )
                    and len(completed) == count,
                    "peak_definition": (
                        "Started owned Docker watchers until confirmed stop; "
                        "Git children from existing product live-child maps. "
                        "Sampling does not reap or signal processes."
                    ),
                },
                indent=2,
            )
        )
        for owner in owners:
            owner.close()
        assert monitor is None or not monitor.is_alive()
        assert not observer_errors, observer_errors


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


def test_observer_failure_is_retained_and_never_counts_as_peak_success(tmp_path, monkeypatch):
    import threading

    from tests.merge_workloads import test_workloads as module

    original = module.Owner

    class BrokenObserver(dict):
        def values(self):
            if threading.current_thread().name == "owned-workload-observer":
                raise RuntimeError("Authored monitor failure")
            return super().values()

    class OwnerWithObserverFault(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            runner = self.capability.checks.checks.source_guard.runner
            runner.jobs = BrokenObserver(runner.jobs)

    monkeypatch.setattr(module, "Owner", OwnerWithObserverFault)
    with pytest.raises(AssertionError, match="Authored monitor failure"):
        module.test_successful_ordered_contenders_keep_every_owned_eight_line_change(tmp_path, 1)
    result = json.loads((tmp_path / "contenders-1-timings.json").read_text())
    assert result["observer_errors"][0]["message"] == "Authored monitor failure"
    assert result["completed"][0]["result"]["data"]["state"] == "done"
    assert result["observed_peaks"]["docker_watchers"] == 0


def test_four_queued_contenders_resume_original_public_keys_after_bounded_turn_refusal(tmp_path):
    test_successful_ordered_contenders_keep_every_owned_eight_line_change(
        tmp_path, 4, resume_waits=True
    )
