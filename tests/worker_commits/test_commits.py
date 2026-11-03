"""Frozen worker-commit controls run against authored projects and actual local Git."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeRefused

from .helpers import WorkerFixture, git


@pytest.fixture
def project(tmp_path):
    fixture = WorkerFixture(tmp_path / "project")
    try:
        yield fixture
    finally:
        fixture.close()


def test_saved_workflow_commits_owned_change_and_keeps_lead_boundary(project):
    result = project.driver.run(project.ref)
    assert result["reason"] == "worker_commit_ready", result
    row = project.store.read(project.ref, project.rig.scope)
    attempt = project.rig.core.get_attempt(project.rig.handle, row["attempt_id"])
    pair = project.runtime.pair(attempt.id)
    receipt = row["steps"]["worker-commit"]["result"]
    assert receipt["commit"] == git(pair.worktree, "rev-parse", "HEAD") != project.base
    assert (
        git(pair.worktree, "show", "HEAD:result.py") == "def total(values):\n    return sum(values)"
    )
    assert git(project.rr.repo, "rev-parse", "HEAD") == project.base
    assert git(pair.worktree, "status", "--porcelain") == ""
    task = project.rig.core.get_task(project.rig.handle, project.task.id)
    assert task.observed_status == "In Progress" and task.completed_work == ()
    assert project.checks.calls == 1 and pair.database.exists()
    assert "deliver" not in row["steps"]
    assert project.driver.run(project.ref)["commit"] == receipt["commit"]
    assert project.checks.calls == 1
    durations = project.rr.journal.observations(attempt.id, "worker-git-result")
    assert {r["stage"] for r in durations} == {"stage", "diff-check", "commit"}
    assert all(r["seconds"] > 0 for r in durations)
    from pathlib import Path

    for stage in durations:
        for output in stage["output"].values():
            path = Path(output["path"])
            assert path.is_file() and path.stat().st_ino == output["inode"]
            assert path.stat().st_size <= 1048576
            assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("alter", ["foreign", "staged", "wrong_branch", "wrong_head", "missing_db"])
def test_scope_and_resource_refusals(project, alter):
    row = project.execute_only()
    pair = project.runtime.pair(row["attempt_id"])
    if alter == "foreign":
        (pair.worktree / "foreign.txt").write_text("Someone else's change\n")
    elif alter == "staged":
        git(pair.worktree, "add", "result.py")
    elif alter == "wrong_branch":
        git(pair.worktree, "switch", "-c", "foreign")
    elif alter == "wrong_head":
        git(pair.worktree, "commit", "--allow-empty", "-qm", "Foreign commit")
    elif alter == "missing_db":
        pair.database.unlink()
    result = project.driver.run(project.ref)
    assert result["state"] == "blocked", result
    assert project.checks.calls == 0
    assert not project.rr.journal.effect(pair.attempt_id, "worker-commit-candidate")


@pytest.mark.parametrize("alter", ["scope", "deadline", "attempt", "omitted_execute"])
def test_stale_authority_and_order_are_refused(project, alter):
    row = project.execute_only()
    if alter == "scope":
        row = dict(row, prose_digest="foreign")
    elif alter == "deadline":
        project.rig.now = project.plan.deadline + 1
    elif alter == "attempt":
        row = dict(row, task_id="foreign")
    elif alter == "omitted_execute":
        row = dict(row, steps={})
    with pytest.raises(Exception):
        project.commits.prepare(row, project.plan)
    assert project.checks.calls == 0


def test_branch_lineage_cannot_be_reused_by_another_task(project):
    row = project.execute_only()
    fact = project.runtime.ownership(row["attempt_id"])
    project.store.lineage(project.task.id, row["attempt_id"], fact)
    with pytest.raises(AccessDenied):
        project.store.lineage("foreign-task", row["attempt_id"], fact)


@pytest.mark.parametrize("failure", ["check", "hook", "whitespace"])
def test_failed_checks_never_commit_or_deliver_and_keep_database(tmp_path, failure):
    f = WorkerFixture(tmp_path / "failure", check_failure=failure == "check")
    try:
        row = f.execute_only()
        pair = f.runtime.pair(row["attempt_id"])
        if failure == "hook":
            hook = f.rr.repo / ".git/hooks/pre-commit"
            hook.write_text("#!/bin/sh\nprintf 'Expected guard refused\\n' >&2\nexit 1\n")
            hook.chmod(0o700)
        if failure == "whitespace":
            (pair.worktree / "result.py").write_text(
                "def total(values):\n    return sum(values)  \n"
            )
        result = f.driver.run(f.ref)
        assert result["state"] == "blocked", result
        assert git(pair.worktree, "rev-parse", "HEAD") == f.base
        assert pair.database.exists()
        if failure == "hook":
            records = f.rr.journal.observations(pair.attempt_id, "worker-git-result")
            failed = [r for r in records if r["stage"] == "commit"][-1]
            from pathlib import Path

            assert "Expected guard refused" in Path(failed["output"]["stderr"]["path"]).read_text()
        with pytest.raises(RuntimeRefused, match="Unresolved worker commit"):
            f.rr.resources.cleanup(f.rig.core, f.rig.handle, pair.attempt_id)
        repeated = f.driver.run(f.ref)
        assert repeated["state"] != "done"
        assert f.checks.calls <= 1
    finally:
        f.close()


@pytest.mark.parametrize("stage", ["after:commit", "after:worker-commit"])
def test_lost_reply_reconciles_actual_commit_without_repeating_hooks(tmp_path, stage):
    class LoseReply:
        def hit(self, name):
            if name == stage:
                raise RuntimeError("Authored lost reply")

    f = WorkerFixture(tmp_path / "reply", faults=LoseReply(), git_faults=LoseReply())
    try:
        audit = tmp_path / "hook-audit"
        hook = f.rr.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook\\n' >> '" + str(audit) + "'\n")
        hook.chmod(0o700)
        first = f.driver.run(f.ref)
        assert first["state"] == "blocked"
        second = f.driver.run(f.ref)
        assert second["reason"] == "worker_commit_ready", second
        assert audit.read_text() == "hook\n" and f.checks.calls == 1
    finally:
        f.close()


def test_interruption_before_commit_is_not_replayed(project):
    class Interrupt:
        def hit(self, name):
            if name == "after:stage":
                raise RuntimeError("Interrupted after staging")

    project.git.faults = Interrupt()
    assert project.driver.run(project.ref)["state"] == "blocked"
    project.git.faults = None
    assert project.driver.run(project.ref)["state"] != "done"
    assert project.checks.calls == 0
    row = project.store.read(project.ref, project.rig.scope)
    pair = project.runtime.pair(row["attempt_id"])
    assert git(pair.worktree, "rev-parse", "HEAD") == project.base
    assert pair.database.exists()


def test_two_workers_commit_concurrently_and_preserve_distinct_branches(tmp_path):
    # Separate authored projects are a control for process/resource isolation.
    fixtures = [WorkerFixture(tmp_path / str(i)) for i in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda f: f.driver.run(f.ref), fixtures))
        assert all(r.get("reason") == "worker_commit_ready" for r in outcomes), outcomes
        assert outcomes[0]["commit"] != outcomes[1]["commit"]
        pairs = []
        for f in fixtures:
            row = f.store.read(f.ref, f.rig.scope)
            pair = f.runtime.pair(row["attempt_id"])
            pairs.append(pair)
            assert pair.database.exists()
            assert git(f.rr.repo, "rev-parse", "HEAD") == f.base
        assert pairs[0].database != pairs[1].database
        assert pairs[0].worktree_id != pairs[1].worktree_id
    finally:
        for f in fixtures:
            f.close()


def test_frozen_control_inventory():
    from pathlib import Path

    inventory = json.loads(Path(__file__).with_name("frozen-v1.json").read_text())
    assert inventory["revision"] == 1 and len(inventory["groups"]) >= 8


def test_same_project_workers_commit_distinct_owned_branches(project):
    second = project.rig.queued()
    second_ref = project.driver.begin(second.id, project.plan.id, "second-worker")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(project.driver.run, (project.ref, second_ref)))
    assert all(o.get("reason") == "worker_commit_ready" for o in outcomes), outcomes
    assert outcomes[0]["commit"] != outcomes[1]["commit"]
    pairs = []
    for reference in (project.ref, second_ref):
        row = project.store.read(reference, project.rig.scope)
        pair = project.runtime.pair(row["attempt_id"])
        pairs.append(pair)
        assert pair.database.exists()
        assert git(pair.worktree, "rev-parse", "HEAD") != project.base
    assert pairs[0].worktree_id != pairs[1].worktree_id
    assert pairs[0].database_id != pairs[1].database_id
    assert git(project.rr.repo, "rev-parse", "HEAD") == project.base


def test_effective_hook_reads_original_owned_database(project, tmp_path):
    import sys

    hooks = tmp_path / "configured-hooks"
    hooks.mkdir()
    audit = tmp_path / "database-audit"
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!" + sys.executable + "\n"
        "import os, sqlite3\n"
        "from pathlib import Path\n"
        "database = os.environ['AITM_WORKER_DATABASE']\n"
        "with sqlite3.connect('file:' + database + '?mode=ro', uri=True) as db:\n"
        "    assert db.execute('SELECT attempt_id FROM fixture_owner').fetchall() == "
        "[(os.environ['AITM_WORKER_ATTEMPT'],)]\n"
        "Path(" + repr(str(audit)) + ").write_text(database)\n"
    )
    hook.chmod(0o700)
    git(project.rr.repo, "config", "core.hooksPath", str(hooks))
    outcome = project.driver.run(project.ref)
    assert outcome.get("reason") == "worker_commit_ready", outcome
    row = project.store.read(project.ref, project.rig.scope)
    pair = project.runtime.pair(row["attempt_id"])
    assert audit.read_text() == str(pair.database) and pair.database.exists()


def test_lost_git_result_reconciles_commit_from_physical_quiescence(project):
    observe = project.rr.journal.observe
    lost = False

    def lose(attempt_id, kind, data):
        nonlocal lost
        if kind == "worker-git-result" and data["stage"] == "commit" and not lost:
            lost = True
            raise RuntimeError("Git completed before its result was persisted")
        return observe(attempt_id, kind, data)

    project.rr.journal.observe = lose
    assert project.driver.run(project.ref)["state"] == "blocked"
    project.rr.journal.observe = observe
    result = project.driver.run(project.ref)
    assert result.get("reason") == "worker_commit_ready", result
    row = project.store.read(project.ref, project.rig.scope)
    assert project.rr.journal.observations(row["attempt_id"], "worker-commit-reconciled-stop")
    assert project.checks.calls == 1


@pytest.mark.parametrize("alter", ["missing_checks", "unverified_checks", "changed_plan"])
def test_omitted_checks_and_changed_plans_refuse(project, alter):
    row = project.execute_only()
    if alter == "missing_checks":
        plan = replace(project.plan, commit_checks=())
        row = dict(row, plan_digest=plan.digest)
    elif alter == "unverified_checks":
        plan = project.plan
        project.commits.checks = object()
    else:
        plan = replace(project.plan, allowed_files=("foreign.py",), artifacts=("foreign.py",))
    with pytest.raises(Exception):
        project.commits.prepare(row, plan)
    assert project.checks.calls == 0


def test_check_cannot_substitute_staged_content(project):
    execute = project.checks.execute_registered

    def substitute(reference, pair, boundary, effect_boundary, operation_key):
        execute(reference, pair, boundary, effect_boundary, operation_key)
        original = (pair.worktree / "result.py").read_bytes()
        (pair.worktree / "result.py").write_text("Unchecked substituted content\n")
        git(pair.worktree, "add", "result.py")
        (pair.worktree / "result.py").write_bytes(original)

    project.checks.execute_registered = substitute
    outcome = project.driver.run(project.ref)
    assert outcome["state"] == "blocked", outcome
    row = project.store.read(project.ref, project.rig.scope)
    pair = project.runtime.pair(row["attempt_id"])
    assert git(pair.worktree, "rev-parse", "HEAD") == project.base
    assert pair.database.exists()


@pytest.mark.parametrize("change", ["deadline", "human_hold", "database", "hook_source"])
def test_authority_is_rechecked_after_checks(project, change):
    execute = project.checks.execute_registered

    def changed(reference, pair, boundary, effect_boundary, operation_key):
        execute(reference, pair, boundary, effect_boundary, operation_key)
        if change == "deadline":
            project.rig.now = project.plan.deadline + 1
        elif change == "human_hold":
            project.rr.hold(project.runtime.dispatches[pair.attempt_id])
        elif change == "database":
            pair.database.unlink()
        else:
            hook = project.rr.repo / ".git/hooks/pre-commit"
            hook.write_text("#!/bin/sh\nexit 0\n")
            hook.chmod(0o700)

    project.checks.execute_registered = changed
    outcome = project.driver.run(project.ref)
    assert outcome["state"] == "blocked", outcome
    row = project.store.read(project.ref, project.rig.scope)
    pair = project.runtime.dispatches[row["attempt_id"]].resources
    assert git(pair.worktree, "rev-parse", "HEAD") == project.base
    assert not project.rr.journal.effect(pair.attempt_id, "worker-commit-candidate")


def test_operation_bound_command_result_does_not_accept_another_invocation(tmp_path):
    from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
    from ai_task_manager.domains.work.errors import AccessDenied

    # The old unqualified API still refuses ambiguous results. Selecting a missing
    # operation never borrows another invocation's successful result.
    runner = object.__new__(DockerCommands)
    runner.jobs = {
        "container:other": {
            "record": {"attempt_id": "a", "command_ref": "c"},
            "verified_ref": "other-result",
        }
    }
    with pytest.raises(AccessDenied, match="One actual successful"):
        runner.verified_result("a", "c", operation_key="wanted")


def test_human_hold_during_hook_stops_commit_and_retains_resources(project, tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor

    marker = tmp_path / "hook-entered"
    hook = project.rr.repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nsleep 20\n")
    hook.chmod(0o700)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(project.driver.run, project.ref)
        deadline = time.monotonic() + 12
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        assert marker.exists()
        row = project.store.read(project.ref, project.rig.scope)
        project.rr.hold(project.runtime.dispatches[row["attempt_id"]])
        outcome = future.result(timeout=8)
    assert outcome["state"] == "blocked", outcome
    pair = project.runtime.dispatches[row["attempt_id"]].resources
    assert git(pair.worktree, "rev-parse", "HEAD") == project.base
    assert pair.database.exists()
    processes = project.rr.journal.observations(pair.attempt_id, "worker-git-result")
    final = [r for r in processes if r["stage"] == "commit"][-1]
    assert final["outcome"] == "interrupted" and final["group"]["confirmed_stop"]
