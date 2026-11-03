"""Positive confined-adapter plumbing controls, without container completion claims.

Git commit-tree authors the private object. These tests exercise real checks,
snapshot/tree/attribution validation and publication, but never create a container
result, StopReceipt or worker_commit_ready receipt. Enabled hooks and descendant
closure require separately registered actual container execution.
"""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.git.commits import ConfinedWorkerGit, WorkerGit
from ai_task_manager.adapters.git.commits.private import environment, file_state, prepare, query
from ai_task_manager.adapters.git.commits.publication import Publication
from ai_task_manager.domains.checks.models import fingerprint
from ai_task_manager.supervisor.models import RuntimeUncertain

from .helpers import WorkerFixture, git


@dataclass
class PrivateCommit:
    fixture: WorkerFixture
    row: dict
    pair: object
    observer: ConfinedWorkerGit
    record: dict
    candidate: dict
    head: str

    @contextmanager
    def authority(self):
        intent = self.candidate
        with self.fixture.rig.core.execution.worker_commit_boundary(
            self.fixture.rig.handle,
            self.row["task_id"],
            self.pair.attempt_id,
            intent["task_version"],
            intent["attempt_version"],
            self.row["decision_revision"],
        ):
            yield

    def publish(self, faults=None):
        return Publication(self.fixture.rr.journal, faults=faults).publish(
            self.pair.attempt_id, self.record, self.head, self.candidate["tree"], self.authority
        )


def private_commit(fixture, reference=None):
    row = fixture.execute_only(reference)
    pair = fixture.runtime.pair(row["attempt_id"])
    # Observation-only construction follows test_index_provenance.py. No runtime
    # capability is installed, and execution/closure methods are never replaced.
    observer = object.__new__(ConfinedWorkerGit)
    WorkerGit.__init__(observer, fixture.rr.journal)
    intent = fixture.commits.prepare(row, fixture.plan)
    intent.update(observer.snapshot(pair.worktree, fixture.plan.allowed_files))
    root = fixture.rr.path / "private-controls" / pair.attempt_id
    root.mkdir(mode=0o700, parents=True)
    record = prepare(observer, pair.attempt_id, pair.worktree, root / "git", intent)
    fixture.rr.journal.intend(pair.attempt_id, "worker-private-git", record)
    env = environment(Path(record["metadata"]), pair.worktree)
    subprocess.run(["git", "add", "--", "result.py"], env=env, check=True, capture_output=True)
    subprocess.run(["git", "diff", "--cached", "--check"], env=env, check=True, capture_output=True)
    control = PrivateCommit(fixture, row, pair, observer, record, intent, "")
    fixture.checks.execute_registered(
        "verify", pair, lambda: None, control.authority, row["id"] + ":private-control"
    )
    assert fixture.checks.verified_result(pair.attempt_id, "verify")["verified"]
    tree = observer.checked_tree(pair.worktree, intent)
    message = (
        f"Worker change for {row['task_id']}\n\nTask: {row['task_id']}\n"
        f"Attempt: {pair.attempt_id}\nWorkflow: {row['id']}\n"
        f"Worker-Intent: {fingerprint(intent)}"
    )
    output = subprocess.run(
        ["git", "commit-tree", tree, "-p", fixture.base],
        input=(message + "\n").encode(),
        env=env,
        check=True,
        capture_output=True,
    )
    head = output.stdout.decode().strip()
    query(record, "update-ref", "HEAD", head, fixture.base)
    candidate = {**intent, "tree": tree, "message": message}
    assert observer._private_commit(record, candidate) == head
    assert git(pair.worktree, "rev-parse", "HEAD") == fixture.base
    return replace(control, candidate=candidate, head=head)


@pytest.fixture
def candidate(tmp_path):
    fixture = WorkerFixture((tmp_path / "private-commit").resolve())
    try:
        yield private_commit(fixture)
    finally:
        fixture.close()


def assert_published(control):
    fixture, pair = control.fixture, control.pair
    assert control.observer.committed(pair.worktree, control.candidate) == control.head
    assert git(pair.worktree, "rev-parse", "HEAD^") == fixture.base
    assert (
        git(pair.worktree, "show", "HEAD:result.py") == "def total(values):\n    return sum(values)"
    )
    assert git(fixture.rr.repo, "rev-parse", "HEAD") == fixture.base
    assert pair.database.exists()
    task = fixture.rig.core.get_task(fixture.rig.handle, control.row["task_id"])
    assert task.observed_status == "In Progress" and task.completed_work == ()
    assert not task.delivered_once
    # Lower-layer publication is not a successful contained workflow receipt.
    assert not fixture.rr.journal.observations(pair.attempt_id, "worker-commit-complete")
    assert not fixture.rr.journal.observations(pair.attempt_id, "worker-git-result")


def test_private_checked_commit_publication_is_exact_and_idempotent(candidate):
    assert candidate.publish() == candidate.head
    assert_published(candidate)
    index = Path(candidate.record["gitdir"]) / "index"
    before = file_state(index)
    journal = candidate.fixture.rr.journal
    kinds = ("worker-objects-imported", "worker-index-lock", "worker-published")
    observations = [journal.observations(candidate.pair.attempt_id, kind) for kind in kinds]
    assert all(len(records) == 1 for records in observations)
    assert candidate.publish() == candidate.head
    assert file_state(index) == before
    assert [journal.observations(candidate.pair.attempt_id, kind) for kind in kinds] == observations
    assert candidate.fixture.checks.calls == 1
    assert_published(candidate)


@pytest.mark.parametrize("cut", ["after:publish-ref", "after:publish-index"])
def test_private_commit_lost_publication_reply_recovers_without_recreating_commit(candidate, cut):
    class LoseReply:
        def hit(self, name):
            if name == cut:
                raise RuntimeError("Authored publication reply loss")

    with pytest.raises(RuntimeError, match="Authored publication"):
        candidate.publish(LoseReply())
    assert git(candidate.pair.worktree, "rev-parse", "HEAD") == candidate.head
    assert candidate.publish() == candidate.head
    assert_published(candidate)
    assert candidate.fixture.checks.calls == 1
    journal = candidate.fixture.rr.journal
    for kind in ("worker-objects-imported", "worker-index-lock", "worker-published"):
        assert len(journal.observations(candidate.pair.attempt_id, kind)) == 1


@pytest.mark.parametrize("field", ["base", "tree", "message"])
def test_private_commit_refuses_changed_parent_tree_or_attribution(candidate, field):
    altered = dict(candidate.candidate, **{field: "unapproved"})
    with pytest.raises(RuntimeUncertain, match="differs from its saved intent"):
        candidate.observer._private_commit(candidate.record, altered)
    assert git(candidate.pair.worktree, "rev-parse", "HEAD") == candidate.fixture.base
    assert not candidate.fixture.rr.journal.observations(
        candidate.pair.attempt_id, "worker-published"
    )


@pytest.mark.parametrize("same_project", [False, True])
def test_private_commits_publish_concurrently_to_distinct_owned_branches(tmp_path, same_project):
    fixtures = [WorkerFixture((tmp_path / "first").resolve())]
    try:
        controls = [private_commit(fixtures[0])]
        if same_project:
            fixture = fixtures[0]
            task = fixture.rig.queued()
            reference = fixture.driver.begin(task.id, fixture.plan.id, "second-private-control")
            controls.append(private_commit(fixture, reference))
        else:
            fixtures.append(WorkerFixture((tmp_path / "second").resolve()))
            controls.append(private_commit(fixtures[1]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            heads = list(pool.map(lambda control: control.publish(), controls))
        assert len(set(heads)) == 2
        assert len({control.pair.worktree_id for control in controls}) == 2
        assert len({control.pair.database_id for control in controls}) == 2
        for control in controls:
            assert_published(control)
        assert sum(fixture.checks.calls for fixture in fixtures) == 2
    finally:
        for fixture in fixtures:
            fixture.close()
