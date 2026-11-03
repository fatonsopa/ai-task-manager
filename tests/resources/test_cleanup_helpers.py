"""Real local Git controls for helpers at the resource-release boundary."""

import os
import shlex

import pytest

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.supervisor.models import RuntimeRefused


def stopped(rr):
    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    return dispatch.resources


def marker_helper(rr, name, body=""):
    marker = rr.path / (name + "-marker")
    helper = rr.path / name
    helper.write_text("#!/bin/sh\nprintf ran >> " + shlex.quote(str(marker)) + "\n" + body)
    helper.chmod(0o700)
    return helper, marker


def remove(rr, pair, entrypoint):
    if entrypoint == "manager":
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, pair.attempt_id)
    else:
        fact = rr.journal.observations(pair.attempt_id, "resources_ready")[-1]
        rr.resources.git.remove(pair.worktree, fact["worktree"])


@pytest.mark.parametrize("entrypoint", ["manager", "adapter"])
def test_cleanup_never_runs_configured_fsmonitor(rr, entrypoint):
    pair = stopped(rr)
    helper, marker = marker_helper(rr, "fsmonitor")
    git(rr.repo, "config", "core.fsmonitor", str(helper))
    # The same configured helper really executes during ordinary project status.
    git(pair.worktree, "status", "--porcelain")
    assert marker.read_text()
    marker.unlink()
    remove(rr, pair, entrypoint)
    assert not marker.exists()
    assert not pair.worktree.exists()
    assert pair.database.exists() == (entrypoint == "adapter")
    assert git(rr.repo, "config", "core.fsmonitor") == str(helper)


@pytest.mark.parametrize("entrypoint", ["manager", "adapter"])
@pytest.mark.parametrize("kind", ["clean", "process"])
def test_cleanup_retains_resources_with_executable_filters(rr, entrypoint, kind):
    pair = stopped(rr)
    (pair.worktree / ".gitattributes").write_text("content.txt filter=fixture\n")
    (pair.worktree / "content.txt").write_text("original\n")
    os.utime(pair.worktree / "content.txt", (1_700_000_000, 1_700_000_000))
    git(pair.worktree, "add", ".")
    git(pair.worktree, "commit", "-qm", "Filtered fixture")
    helper, marker = marker_helper(rr, "filter", "cat\n" if kind == "clean" else "exit 1\n")
    git(rr.repo, "config", "filter.fixture." + kind, str(helper))
    # Make the index stat cache stale so status must inspect the contents.
    (pair.worktree / "content.txt").write_text("modified\n")
    if kind == "process":
        with pytest.raises(RuntimeRefused):
            git(pair.worktree, "status", "--porcelain")
    else:
        git(pair.worktree, "status", "--porcelain")
    assert marker.read_text()
    marker.unlink()
    with pytest.raises(RuntimeRefused, match="filter"):
        remove(rr, pair, entrypoint)
    assert not marker.exists()
    assert pair.worktree.exists() and pair.database.exists()
    assert (pair.worktree / "content.txt").read_text() == "modified\n"
    assert git(rr.repo, "config", "filter.fixture." + kind) == str(helper)
