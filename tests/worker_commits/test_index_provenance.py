"""Real local Git controls for the index observed before private staging."""

import subprocess
from pathlib import Path

import pytest

from ai_task_manager.adapters.git.commits import ConfinedWorkerGit, WorkerGit
from ai_task_manager.adapters.git.commits.private import environment, file_state, prepare, query
from ai_task_manager.domains.work.errors import AccessDenied

from .helpers import WorkerFixture, git


@pytest.fixture
def owned(tmp_path):
    fixture = WorkerFixture((tmp_path / "index-provenance").resolve())
    row = fixture.execute_only()
    pair = fixture.runtime.pair(row["attempt_id"])
    # Exercise observation and validation only; no container capability or receipt is supplied.
    observer = object.__new__(ConfinedWorkerGit)
    WorkerGit.__init__(observer, fixture.rr.journal)
    try:
        yield fixture, pair, observer
    finally:
        fixture.close()


def foreign_stage(pair):
    name = "verify.py"
    value = b"Concurrent cached-only change.\n"
    blob = subprocess.run(
        ["git", "-C", str(pair.worktree), "hash-object", "-w", "--stdin"],
        input=value,
        capture_output=True,
        check=True,
    ).stdout.decode().strip()
    git(pair.worktree, "update-index", "--cacheinfo", f"100644,{blob},{name}")
    gitdir = Path(git(pair.worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
    return file_state(gitdir / "index"), value.decode().strip()


def intent_for(fixture, pair, observer):
    return {
        "attempt_id": pair.attempt_id,
        "base": fixture.base,
        **observer.snapshot(pair.worktree, ("result.py",)),
    }


def metadata_root(fixture):
    root = fixture.rr.path / "private"
    root.mkdir(mode=0o700)
    return root / "git"


def test_concurrent_index_before_snapshot_is_refused(owned):
    fixture, pair, observer = owned
    state, value = foreign_stage(pair)
    with pytest.raises(AccessDenied, match="Preexisting staged"):
        intent_for(fixture, pair, observer)
    gitdir = Path(git(pair.worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
    assert file_state(gitdir / "index") == state
    assert git(pair.worktree, "show", ":verify.py") == value


def test_concurrent_index_during_snapshot_is_refused(owned):
    fixture, pair, observer = owned
    original = observer.query
    changed = []

    def interleave(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if args[:1] == ("ls-files",) and not changed:
            changed.append(foreign_stage(pair))
        return result

    observer.query = interleave
    with pytest.raises(AccessDenied, match="index changed during"):
        intent_for(fixture, pair, observer)
    assert changed
    assert git(pair.worktree, "show", ":verify.py") == changed[0][1]


def test_concurrent_index_between_snapshot_and_preparation_is_preserved(owned):
    fixture, pair, observer = owned
    intent = intent_for(fixture, pair, observer)
    state, value = foreign_stage(pair)
    directory = metadata_root(fixture)
    with pytest.raises(AccessDenied, match="index changed since"):
        prepare(observer, pair.attempt_id, pair.worktree, directory, intent)
    assert not directory.exists()
    gitdir = Path(git(pair.worktree, "rev-parse", "--path-format=absolute", "--git-dir"))
    assert file_state(gitdir / "index") == state
    assert git(pair.worktree, "show", ":verify.py") == value
    assert git(pair.worktree, "rev-parse", "HEAD") == fixture.base
    assert not fixture.rr.journal.effect(pair.attempt_id, "worker-private-git")


def test_preparation_cannot_adopt_an_intent_without_checked_index(owned):
    fixture, pair, observer = owned
    intent = intent_for(fixture, pair, observer)
    intent.pop("original_index", None)
    directory = metadata_root(fixture)
    with pytest.raises(AccessDenied, match="Checked original index"):
        prepare(observer, pair.attempt_id, pair.worktree, directory, intent)
    assert not directory.exists()


def test_concurrent_index_during_private_preparation_is_refused(owned, monkeypatch):
    from ai_task_manager.adapters.git.commits import private

    fixture, pair, observer = owned
    intent = intent_for(fixture, pair, observer)
    original = private.query
    changed = []

    def interleave(record, *args, **kwargs):
        result = original(record, *args, **kwargs)
        if args[:1] == ("read-tree",):
            changed.append(foreign_stage(pair))
        return result

    monkeypatch.setattr(private, "query", interleave)
    directory = metadata_root(fixture)
    with pytest.raises(AccessDenied, match="index changed during"):
        prepare(observer, pair.attempt_id, pair.worktree, directory, intent)
    assert changed
    assert git(pair.worktree, "show", ":verify.py") == changed[0][1]
    assert not (directory.parent / "preparation.json").exists()


def test_concurrent_index_after_preparation_blocks_checked_tree(owned):
    fixture, pair, observer = owned
    intent = intent_for(fixture, pair, observer)
    record = prepare(observer, pair.attempt_id, pair.worktree, metadata_root(fixture), intent)
    fixture.rr.journal.intend(pair.attempt_id, "worker-private-git", record)
    env = environment(Path(record["metadata"]), pair.worktree)
    subprocess.run(["git", "add", "--", "result.py"], env=env, check=True, capture_output=True)
    _, value = foreign_stage(pair)
    with pytest.raises(AccessDenied, match="Original index changed"):
        observer.checked_tree(pair.worktree, intent)
    assert git(pair.worktree, "show", ":verify.py") == value


def test_unchanged_snapshot_index_is_retained_for_publication(owned):
    fixture, pair, observer = owned
    intent = intent_for(fixture, pair, observer)
    record = prepare(observer, pair.attempt_id, pair.worktree, metadata_root(fixture), intent)
    fixture.rr.journal.intend(pair.attempt_id, "worker-private-git", record)
    assert record["original_index"] == intent["original_index"]
    env = environment(Path(record["metadata"]), pair.worktree)
    subprocess.run(["git", "add", "--", "result.py"], env=env, check=True, capture_output=True)
    expected = query(record, "write-tree").decode().strip()
    assert observer.checked_tree(pair.worktree, intent) == expected
