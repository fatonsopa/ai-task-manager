"""Release configuration, final removal and foreign-content controls."""

from pathlib import Path

import pytest

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.supervisor.models import RuntimeRefused

from .test_cleanup_helpers import marker_helper, remove, stopped


def worktree_config(rr, pair, key, value):
    git(rr.repo, "config", "extensions.worktreeConfig", "true")
    git(pair.worktree, "config", "--worktree", key, value)


def test_final_removal_disables_target_worktree_monitor_and_hooks(rr, monkeypatch):
    pair = stopped(rr)
    monitor, marker = marker_helper(rr, "target-monitor")
    hooks = rr.path / "project-hooks"
    hooks.mkdir()
    hook, hook_marker = marker_helper(rr, "reference-transaction")
    hook.rename(hooks / hook.name)
    worktree_config(rr, pair, "core.fsmonitor", str(monitor))
    worktree_config(rr, pair, "core.hooksPath", str(hooks))
    git(pair.worktree, "status", "--porcelain")
    git(pair.worktree, "update-ref", "refs/heads/probe", "HEAD")
    assert marker.exists() and hook_marker.exists()
    marker.unlink()
    hook_marker.unlink()
    # Isolate Git's own final cleanliness check from the adapter's earlier one.
    monkeypatch.setattr(rr.resources.git, "require_clean", lambda path: None)
    remove(rr, pair, "adapter")
    assert not marker.exists() and not hook_marker.exists()
    assert not pair.worktree.exists()
    assert git(rr.repo, "rev-parse", "refs/heads/probe")


@pytest.mark.parametrize("scope", ["include", "worktree"])
def test_release_reads_filter_configuration_from_includes_and_worktrees(rr, scope):
    pair = stopped(rr)
    helper, marker = marker_helper(rr, "included-filter", "cat\n")
    if scope == "worktree":
        worktree_config(rr, pair, "filter.fixture.clean", str(helper))
    else:
        config = rr.path / "included-config"
        git(rr.repo, "config", "--file", str(config), "filter.fixture.clean", str(helper))
        git(rr.repo, "config", "include.path", str(config))
    with pytest.raises(RuntimeRefused, match="filter"):
        remove(rr, pair, "manager")
    assert not marker.exists()
    assert pair.worktree.exists() and pair.database.exists()


def test_final_removal_rechecks_target_filter_configuration(rr, monkeypatch):
    pair = stopped(rr)
    helper, marker = marker_helper(rr, "late-filter", "cat\n")
    original = rr.resources.git.require_clean

    def install_filter(path):
        original(path)
        worktree_config(rr, pair, "filter.fixture.process", str(helper))

    monkeypatch.setattr(rr.resources.git, "require_clean", install_filter)
    with pytest.raises(RuntimeRefused, match="filter"):
        remove(rr, pair, "adapter")
    assert not marker.exists()
    assert pair.worktree.exists() and pair.database.exists()


def test_submodule_content_is_retained_without_nested_helper_execution(rr):
    pair = stopped(rr)
    nested = pair.worktree / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    git(nested, "config", "user.name", "Fixture")
    git(nested, "config", "user.email", "fixture@example.invalid")
    git(nested, "commit", "--allow-empty", "-qm", "Nested fixture")
    git(pair.worktree, "update-index", "--add", "--cacheinfo",
        "160000," + git(nested, "rev-parse", "HEAD") + ",nested")
    git(pair.worktree, "commit", "-qm", "Gitlink fixture")
    helper, marker = marker_helper(rr, "nested-monitor")
    git(nested, "config", "core.fsmonitor", str(helper))
    sentinel = nested / "uncommitted.txt"
    sentinel.write_text("retain nested content\n")
    with pytest.raises(RuntimeRefused, match="submodule"):
        remove(rr, pair, "manager")
    assert not marker.exists()
    assert sentinel.read_text() == "retain nested content\n"
    assert pair.database.exists()


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked"])
def test_dirty_content_is_retained_with_monitor_disabled(rr, change):
    pair = stopped(rr)
    tracked = pair.worktree / "owned.txt"
    tracked.write_text("original\n")
    git(pair.worktree, "add", "owned.txt")
    git(pair.worktree, "commit", "-qm", "Owned content")
    helper, marker = marker_helper(rr, "dirty-monitor")
    git(rr.repo, "config", "core.fsmonitor", str(helper))
    if change == "untracked":
        tracked = pair.worktree / "foreign.txt"
    tracked.write_text("preserve me\n")
    if change == "staged":
        git(pair.worktree, "-c", "core.fsmonitor=false", "add", "owned.txt")
    with pytest.raises(RuntimeRefused, match="Uncommitted"):
        remove(rr, pair, "manager")
    assert not marker.exists()
    assert tracked.read_text() == "preserve me\n"
    assert pair.database.exists()


def test_release_does_not_inherit_git_routing_or_command_configuration(rr, monkeypatch):
    pair = stopped(rr)
    foreign = rr.start().resources
    helper, marker = marker_helper(rr, "environment-monitor")
    monkeypatch.setenv("GIT_DIR", str(rr.repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign.worktree))
    monkeypatch.setenv("GIT_INDEX_FILE", str(rr.path / "foreign-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(helper))
    remove(rr, pair, "manager")
    assert not marker.exists()
    assert not pair.worktree.exists() and not pair.database.exists()
    assert foreign.worktree.exists() and foreign.database.exists()
    assert not Path(rr.path / "foreign-index").exists()


def test_partial_repository_release_is_refused_before_observations(rr):
    pair = stopped(rr)
    git(rr.repo, "config", "remote.fixture.promisor", "true")
    with pytest.raises(RuntimeRefused, match="Partial"):
        remove(rr, pair, "manager")
    assert pair.worktree.exists() and pair.database.exists()
