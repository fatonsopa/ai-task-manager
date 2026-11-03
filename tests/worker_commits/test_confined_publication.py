"""Actual Git publication plumbing controls; these do not attest container closure."""

import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest

from ai_task_manager.adapters.git.commits import ConfinedWorkerGit, WorkerGit
from ai_task_manager.adapters.git.commits.private import environment, file_state, prepare, query
from ai_task_manager.adapters.git.commits.publication import Publication
from ai_task_manager.supervisor.models import RuntimeUncertain

from .helpers import WorkerFixture, git


@pytest.fixture
def prepared(tmp_path):
    fixture = WorkerFixture((tmp_path / "publication").resolve())
    row = fixture.execute_only()
    pair = fixture.runtime.pair(row["attempt_id"])
    observer = WorkerGit(fixture.rr.journal)
    intent = fixture.commits.prepare(row, fixture.plan)
    intent.update(ConfinedWorkerGit.snapshot(observer, pair.worktree, fixture.plan.allowed_files))
    root = fixture.rr.path / "private"
    root.mkdir(mode=0o700)
    record = prepare(observer, pair.attempt_id, pair.worktree, root / "git", intent)
    original_hooks = fixture.rr.repo / ".git/hooks"
    assert {p.relative_to(original_hooks) for p in original_hooks.rglob("*") if p.is_file()} == {
        p.relative_to(root / "git/hooks") for p in (root / "git/hooks").rglob("*") if p.is_file()
    }
    env = environment(Path(record["metadata"]), pair.worktree)
    subprocess.run(["git", "add", "--", "result.py"], env=env, check=True, capture_output=True)
    tree = query(record, "write-tree").decode().strip()
    # A plumbing-authored object exercises publication only; no success/closure receipt is forged.
    process = subprocess.run(
        ["git", "commit-tree", tree, "-p", fixture.base],
        input=b"Publication control\n",
        env=env,
        check=True,
        capture_output=True,
    )
    head = process.stdout.decode().strip()
    try:
        yield fixture, pair, record, tree, head
    finally:
        fixture.close()


def test_publication_updates_only_owned_ref_and_original_index(prepared):
    fixture, pair, record, tree, head = prepared
    git(fixture.rr.repo, "branch", "foreign", fixture.base)
    refs = git(fixture.rr.repo, "show-ref")
    Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, nullcontext)
    assert git(pair.worktree, "rev-parse", "HEAD") == head
    assert git(pair.worktree, "status", "--porcelain") == ""
    assert git(fixture.rr.repo, "rev-parse", "HEAD") == fixture.base
    assert git(fixture.rr.repo, "rev-parse", "foreign") == fixture.base
    assert "refs/heads/foreign" in refs
    assert fixture.rr.journal.observations(pair.attempt_id, "worker-published")


@pytest.mark.parametrize("cut", ["after:publish-ref", "after:publish-index"])
def test_restart_finishes_exact_publication_without_replaying_git_commit(prepared, cut):
    fixture, pair, record, tree, head = prepared

    class Lose:
        def hit(self, name):
            if name == cut:
                raise RuntimeError("Lost host publication reply")

    with pytest.raises(RuntimeError, match="Lost host"):
        Publication(fixture.rr.journal, faults=Lose()).publish(
            pair.attempt_id, record, head, tree, nullcontext
        )
    assert git(pair.worktree, "rev-parse", "HEAD") == head
    Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, nullcontext)
    assert git(pair.worktree, "status", "--porcelain") == ""
    assert not (Path(record["gitdir"]) / "index.lock").exists()
    assert len(fixture.rr.journal.observations(pair.attempt_id, "worker-published")) == 1


def test_foreign_index_change_is_preserved_and_blocks_branch_cas(prepared):
    fixture, pair, record, tree, head = prepared
    (pair.worktree / "foreign.txt").write_text("Foreign staged work\n")
    git(pair.worktree, "add", "foreign.txt")
    index = Path(record["gitdir"]) / "index"
    foreign = file_state(index)
    with pytest.raises(RuntimeUncertain, match="Original index changed"):
        Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, nullcontext)
    assert file_state(index) == foreign
    assert git(pair.worktree, "rev-parse", "HEAD") == fixture.base
    assert (pair.worktree / "foreign.txt").read_text() == "Foreign staged work\n"


def test_foreign_index_after_ref_commit_is_not_overwritten_on_recovery(prepared):
    fixture, pair, record, tree, head = prepared

    class Lose:
        def hit(self, name):
            if name == "after:publish-ref":
                raise RuntimeError("Lost after CAS")

    with pytest.raises(RuntimeError):
        Publication(fixture.rr.journal, faults=Lose()).publish(
            pair.attempt_id, record, head, tree, nullcontext
        )
    index = Path(record["gitdir"]) / "index"
    # A writer that ignores the existing Git lock is still preserved on restart.
    index.write_bytes(index.read_bytes() + b"foreign")
    foreign = file_state(index)
    with pytest.raises(RuntimeUncertain, match="Original index changed"):
        Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, nullcontext)
    assert file_state(index) == foreign
    assert git(pair.worktree, "rev-parse", "HEAD") == head


def test_foreign_lock_is_never_removed_or_adopted(prepared):
    fixture, pair, record, tree, head = prepared
    lock = Path(record["gitdir"]) / "index.lock"
    lock.write_text("Foreign writer\n")
    with pytest.raises(RuntimeUncertain, match="Foreign or unjournaled"):
        Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, nullcontext)
    assert lock.read_text() == "Foreign writer\n"
    assert git(pair.worktree, "rev-parse", "HEAD") == fixture.base


def test_missing_current_authority_cannot_import_objects_or_create_shared_locks(prepared):
    from contextlib import contextmanager

    from ai_task_manager.domains.work.errors import AccessDenied

    fixture, pair, record, tree, head = prepared
    objects = Path(record["objects"])
    before = {
        str(p.relative_to(objects)): p.read_bytes() for p in objects.rglob("*") if p.is_file()
    }
    index = file_state(Path(record["gitdir"]) / "index")

    @contextmanager
    def denied():
        raise AccessDenied("Current authority was withdrawn")
        yield

    with pytest.raises(AccessDenied, match="withdrawn"):
        Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, denied)
    assert {
        str(p.relative_to(objects)): p.read_bytes() for p in objects.rglob("*") if p.is_file()
    } == before
    assert file_state(Path(record["gitdir"]) / "index") == index
    assert not (Path(record["gitdir"]) / "index.lock").exists()
    assert git(pair.worktree, "rev-parse", "HEAD") == fixture.base


def test_snapshot_command_authority_cannot_authorize_actual_resource_git():
    from ai_task_manager.application.service.models import Unavailable
    from ai_task_manager.installation.workflows.registry import NativeAuthorities

    with pytest.raises(Unavailable, match="explicit actual-resource Git"):
        NativeAuthorities(None, None, None, "", worker_git=object()).require()


def test_confined_snapshot_never_executes_configured_worktree_filters(tmp_path):
    import os
    import shlex
    import sys

    from ai_task_manager.adapters.git.commits import ConfinedWorkerGit

    path = tmp_path.resolve() / "filters"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    (path / ".gitattributes").write_text("value.py filter=fixture\n")
    (path / "value.py").write_text("old\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "Filter observation baseline")
    marker = path / "filter-was-executed"
    program = f"from pathlib import Path;Path({str(marker)!r}).write_text('unsafe');print('old')"
    git(
        path,
        "config",
        "filter.fixture.clean",
        shlex.quote(sys.executable) + " -c " + shlex.quote(program),
    )
    (path / "value.py").write_text("new\n")
    observer = WorkerGit(None)
    observer.environment = {"PATH": os.defpath, "HOME": str(path), "LC_ALL": "C"}
    # Exercise the read-only method directly. No container result or commit is supplied.
    snapshot = ConfinedWorkerGit.snapshot(observer, path, ("value.py",))
    assert snapshot["files"] == ["value.py"]
    assert not marker.exists()


def test_private_hooks_cannot_change_between_preparation_and_host_observation(prepared):
    from ai_task_manager.adapters.git.commits.private import verify
    from ai_task_manager.domains.work.errors import AccessDenied

    _, _, record, _, _ = prepared
    hook = Path(record["metadata"]) / "hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    with pytest.raises(AccessDenied, match="configuration, hooks or object lineage changed"):
        verify(record)


def test_git_waits_and_import_io_do_not_hold_the_core_effect_gate(prepared, monkeypatch):
    from contextlib import contextmanager

    fixture, pair, record, tree, head = prepared
    state = {"inside": False, "entries": 0}

    @contextmanager
    def gate():
        assert not state["inside"]
        state["inside"] = True
        state["entries"] += 1
        try:
            yield
        finally:
            state["inside"] = False

    original_communicate = subprocess.Popen.communicate
    original_wait = subprocess.Popen.wait
    original_reply = Publication._reply

    def communicate(process, *args, **kwargs):
        assert not state["inside"], "Git input/output must run outside the Core transaction"
        return original_communicate(process, *args, **kwargs)

    def wait(process, *args, **kwargs):
        assert not state["inside"], "Waiting for Git must not hold the Core transaction"
        return original_wait(process, *args, **kwargs)

    def reply(publication, process, expected):
        assert not state["inside"], "Git acknowledgement wait must run outside Core"
        return original_reply(publication, process, expected)

    monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    monkeypatch.setattr(Publication, "_reply", reply)
    Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, gate)
    assert state["entries"] >= 8
    assert git(pair.worktree, "rev-parse", "HEAD") == head


def test_authority_loss_after_import_keeps_objects_unreachable_and_original_index(prepared):
    from contextlib import contextmanager

    from ai_task_manager.domains.work.errors import AccessDenied

    fixture, pair, record, tree, head = prepared
    index = file_state(Path(record["gitdir"]) / "index")
    count = 0

    @contextmanager
    def withdraw():
        nonlocal count
        count += 1
        if count == 2:
            raise AccessDenied("Authority changed while trusted object import was running")
        yield

    with pytest.raises(AccessDenied, match="Authority changed"):
        Publication(fixture.rr.journal).publish(pair.attempt_id, record, head, tree, withdraw)
    assert file_state(Path(record["gitdir"]) / "index") == index
    assert not (Path(record["gitdir"]) / "index.lock").exists()
    assert git(pair.worktree, "rev-parse", "HEAD") == fixture.base
    assert not fixture.rr.journal.observations(pair.attempt_id, "worker-published")


@pytest.mark.parametrize("adapter", [None, "legacy_host"])
def test_code_installation_refuses_missing_or_unconfined_worker_git_authority(adapter):
    from ai_task_manager.application.service.models import Unavailable
    from ai_task_manager.installation.workflows.registry import NativeAuthorities

    value = WorkerGit(None) if adapter else None
    authority = NativeAuthorities(None, None, None, "", worker_git=value)
    with pytest.raises(Unavailable, match="explicit contained worker Git authority"):
        authority.require_worker_git(None)
