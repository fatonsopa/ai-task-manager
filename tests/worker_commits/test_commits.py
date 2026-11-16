"""Host controls using actual local Git; historical IDs follow frozen-v6.json oracles."""

import ast
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from ai_task_manager.adapters.git.commits.private import file_state
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


def assert_host_refusal(fixture, outcome, reference=None):
    """An actual host commit grants neither descendant closure nor task delivery."""
    assert outcome["state"] == "uncertain", outcome
    assert outcome["reason"] == "RuntimeUncertain", outcome
    assert "verified descendant containment" in outcome["detail"], outcome
    row = fixture.store.read(reference or fixture.ref, fixture.rig.scope)
    pair = fixture.runtime.pair(row["attempt_id"])
    task = fixture.rig.core.get_task(fixture.rig.handle, row["task_id"])
    assert task.observed_status == "In Progress" and task.completed_work == ()
    assert not task.delivered_once and "deliver" not in row["steps"]
    assert row["steps"]["worker-commit"]["result"] is None
    assert not fixture.rr.journal.observations(pair.attempt_id, "worker-commit-complete")
    assert not fixture.rr.journal.observations(pair.attempt_id, "worker-commit-reconciled-stop")
    assert pair.database.exists() and pair.worktree.exists()
    assert git(fixture.rr.repo, "rev-parse", "HEAD") == fixture.base
    with pytest.raises(RuntimeRefused, match="Unresolved worker commit"):
        fixture.rr.resources.cleanup(fixture.rig.core, fixture.rig.handle, pair.attempt_id)
    return row, pair


def host_state(fixture, row, pair):
    """Capture effects that an uncertain retry must preserve exactly."""
    gitdir = Path(git(pair.worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
    return {
        "refs": git(fixture.rr.repo, "show-ref"),
        "index": file_state(gitdir / "index"),
        "database": file_state(pair.database),
        "worktree": {
            str(path.relative_to(pair.worktree)): path.read_bytes()
            for path in pair.worktree.rglob("*")
            if path.is_file()
        },
        "task": fixture.rig.core.get_task(fixture.rig.handle, row["task_id"]),
        "attempt": fixture.rig.core.get_attempt(fixture.rig.handle, pair.attempt_id),
        "checks": fixture.checks.calls,
        "git_results": fixture.rr.journal.observations(pair.attempt_id, "worker-git-result"),
        "candidate": fixture.rr.journal.effect(pair.attempt_id, "worker-commit-candidate"),
    }


def assert_retry_preserves_host_state(fixture, row, pair, reference=None):
    before = host_state(fixture, row, pair)
    repeated = fixture.driver.run(reference or fixture.ref)
    assert_host_refusal(fixture, repeated, reference)
    assert host_state(fixture, row, pair) == before


def test_saved_workflow_commits_owned_change_and_keeps_lead_boundary(project):
    result = project.driver.run(project.ref)
    row, pair = assert_host_refusal(project, result)
    attempt = project.rig.core.get_attempt(project.rig.handle, row["attempt_id"])
    candidate = project.rr.journal.effect(attempt.id, "worker-commit-candidate")
    assert project.git.committed(pair.worktree, candidate) != project.base
    assert (
        git(pair.worktree, "show", "HEAD:result.py") == "def total(values):\n    return sum(values)"
    )
    assert git(project.rr.repo, "rev-parse", "HEAD") == project.base
    assert git(pair.worktree, "status", "--porcelain") == ""
    task = project.rig.core.get_task(project.rig.handle, project.task.id)
    assert task.observed_status == "In Progress" and task.completed_work == ()
    assert project.checks.calls == 1 and pair.database.exists()
    assert "deliver" not in row["steps"]
    assert_retry_preserves_host_state(project, row, pair)
    assert project.checks.calls == 1
    durations = project.rr.journal.observations(attempt.id, "worker-git-result")
    assert {r["stage"] for r in durations} == {"stage", "diff-check", "candidate-index", "commit"}
    assert all(r["seconds"] > 0 for r in durations)
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
    hits = []

    class LoseReply:
        def hit(self, name):
            if name == stage:
                hits.append(name)
                raise RuntimeError("Authored lost reply")

    f = WorkerFixture(tmp_path / "reply", faults=LoseReply(), git_faults=LoseReply())
    try:
        audit = tmp_path / "hook-audit"
        hook = f.rr.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook\\n' >> '" + str(audit) + "'\n")
        hook.chmod(0o700)
        first = f.driver.run(f.ref)
        assert first["state"] == ("blocked" if stage == "after:commit" else "uncertain")
        assert hits == ([stage] if stage == "after:commit" else [])
        # The workflow-completion fault is unreachable without containment proof.
        row = f.store.read(f.ref, f.rig.scope)
        pair = f.runtime.pair(row["attempt_id"])
        head = git(pair.worktree, "rev-parse", "HEAD")
        assert head != f.base
        second = f.driver.run(f.ref)
        assert_host_refusal(f, second)
        assert git(pair.worktree, "rev-parse", "HEAD") == head
        assert audit.read_text() == "hook\n" and f.checks.calls == 1
        assert_retry_preserves_host_state(f, row, pair)
        assert audit.read_text() == "hook\n"
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
        pairs = []
        for f, outcome in zip(fixtures, outcomes, strict=True):
            row, pair = assert_host_refusal(f, outcome)
            pairs.append(pair)
            assert git(pair.worktree, "rev-parse", "HEAD") != f.base
            assert pair.database.exists()
            assert git(f.rr.repo, "rev-parse", "HEAD") == f.base
            assert_retry_preserves_host_state(f, row, pair)
        assert git(pairs[0].worktree, "rev-parse", "HEAD") != git(
            pairs[1].worktree, "rev-parse", "HEAD"
        )
        assert pairs[0].database != pairs[1].database
        assert pairs[0].worktree_id != pairs[1].worktree_id
    finally:
        for f in fixtures:
            f.close()


def test_frozen_control_inventory():
    inventory = json.loads(Path(__file__).with_name("frozen-v1.json").read_text())
    assert inventory["revision"] == 1 and len(inventory["groups"]) >= 8
    current = json.loads(Path(__file__).with_name("frozen-v6.json").read_text())
    assert current["revision"] == 6
    assert len(current["reconciliations"]) == 7
    assert len({case["historical_id"] for case in current["reconciliations"]}) == 7
    assert current["preserved_versions"] == [f"frozen-v{i}.json" for i in range(1, 6)]
    for name, digest in current["preserved_sha256"].items():
        assert hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() == digest
    root = Path(__file__).resolve().parents[2]
    for case in current["reconciliations"]:
        assert case["historical_id"] == case["current_host_case"]
        assert case["intent"] and case["decision"] and case["positive_component_cases"]
        for nodeid in [
            case["current_host_case"],
            *case["positive_component_cases"],
            *case.get("related_guard_cases", []),
        ]:
            filename, name = nodeid.split("::")
            functions = {
                node.name
                for node in ast.parse((root / filename).read_text()).body
                if isinstance(node, ast.FunctionDef)
            }
            assert name.split("[")[0] in functions


def test_later_worker_cannot_start_before_earlier_eligible_task(project):
    second = project.rig.queued()
    reference = project.driver.begin(second.id, project.plan.id, "later-worker")
    before = [
        project.rig.core.get_task(project.rig.handle, task_id)
        for task_id in (project.task.id, second.id)
    ]
    outcome = project.driver.run(reference)
    assert outcome["state"] == "blocked", outcome
    assert outcome["reason"] == "SchedulingBlocked", outcome
    assert outcome["detail"] == "An earlier eligible queue item has priority"
    assert project.store.read(reference, project.rig.scope)["attempt_id"] is None
    assert project.runtime.dispatches == {}
    assert project.checks.calls == 0
    assert [project.rig.core.get_task(project.rig.handle, task.id) for task in before] == before
    assert git(project.rr.repo, "rev-parse", "HEAD") == project.base


def test_same_project_workers_commit_distinct_owned_branches(project, monkeypatch):
    second = project.rig.queued()
    second_ref = project.driver.begin(second.id, project.plan.id, "second-worker")
    # Respect queue order during startup; this case targets simultaneous commits.
    for reference in (project.ref, second_ref):
        row = project.execute_only(reference)
        assert row["steps"]["execute"]["result"]["state"] == "done"
        assert "worker-commit" not in row["steps"]
    together = Barrier(2, timeout=10)
    staged_attempts = set()
    stage = project.git.stage

    def simultaneous_stage(attempt_id, *args, **kwargs):
        staged_attempts.add(attempt_id)
        together.wait()
        return stage(attempt_id, *args, **kwargs)

    monkeypatch.setattr(project.git, "stage", simultaneous_stage)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(project.driver.run, (project.ref, second_ref)))
    assert len(staged_attempts) == 2
    pairs = []
    for reference, outcome in zip((project.ref, second_ref), outcomes, strict=True):
        row, pair = assert_host_refusal(project, outcome, reference)
        pairs.append(pair)
        assert pair.database.exists()
        assert git(pair.worktree, "rev-parse", "HEAD") != project.base
        assert_retry_preserves_host_state(project, row, pair, reference)
    assert git(pairs[0].worktree, "rev-parse", "HEAD") != git(
        pairs[1].worktree, "rev-parse", "HEAD"
    )
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
    row, pair = assert_host_refusal(project, outcome)
    assert audit.read_text() == str(pair.database) and pair.database.exists()
    assert_retry_preserves_host_state(project, row, pair)
    assert audit.read_text() == str(pair.database)


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
    row, pair = assert_host_refusal(project, result)
    assert lost and project.checks.calls == 1
    assert git(pair.worktree, "rev-parse", "HEAD") != project.base
    assert not [
        r
        for r in project.rr.journal.observations(pair.attempt_id, "worker-git-result")
        if r["stage"] == "commit"
    ]
    assert_retry_preserves_host_state(project, row, pair)


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


@pytest.mark.parametrize("change", ["none", "content", "legacy"])
def test_pending_workflow_commit_before_component_intent_continues_owned_work(project, change):
    row = project.execute_only()
    pair = project.runtime.pair(row["attempt_id"])
    intent = project.commits.prepare(row, project.plan)
    if change == "legacy":
        intent.pop("commit_protocol")
    owner = "original-before-component"
    project.store.claim(project.ref, project.rig.scope, owner)
    project.store.intend(project.ref, owner, "worker-commit", intent)
    project.store.release(project.ref, owner)
    inode = pair.database.stat().st_ino
    original = (pair.worktree / "result.py").read_bytes()
    if change == "content":
        (pair.worktree / "result.py").write_text("Retained changed work\n")
    result = project.driver.run(project.ref)
    if change == "legacy":
        assert result["state"] == "uncertain", result
        assert project.checks.calls == 0
        assert project.rr.journal.effect(pair.attempt_id, "worker-commit-intent") is None
        assert (pair.worktree / "result.py").read_bytes() == original
        return
    if change == "content":
        assert result["state"] == "blocked" and result["reason"] == "AccessDenied", result
        assert project.checks.calls == 0
        assert (pair.worktree / "result.py").read_text() == "Retained changed work\n"
        assert project.rr.journal.effect(pair.attempt_id, "worker-commit-intent") is None
        assert pair.database.stat().st_ino == inode
        return
    assert_host_refusal(project, result)
    assert project.checks.calls == 1
    assert pair.database.stat().st_ino == inode
    assert (pair.worktree / "result.py").read_bytes() == original
    assert len(project.rr.runtime.workers) == 1
