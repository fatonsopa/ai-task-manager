"""Version 3 regressions for immutable trees and unconfined hook descendants."""

import os
import sys
import time

import pytest

from ai_task_manager.adapters.git.commits import WorkerGit
from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeUncertain

from .helpers import WorkerFixture, git


def production_fixture(path):
    fixture = WorkerFixture(path)
    fixture.git = WorkerGit(fixture.rr.journal)
    fixture.commits.git = fixture.git
    return fixture


def test_foreign_index_race_at_tree_capture_is_refused(tmp_path):
    fixture = production_fixture(tmp_path / "race")
    try:
        original_tree = fixture.git.tree

        def interleave(path):
            (path / "foreign.txt").write_text("Outside the assigned file set.\n")
            git(path, "add", "--", "foreign.txt")
            return original_tree(path)

        fixture.git.tree = interleave
        result = fixture.driver.run(fixture.ref)
        row = fixture.store.read(fixture.ref, fixture.rig.scope)
        pair = fixture.runtime.pair(row["attempt_id"])
        assert result["state"] == "blocked", result
        assert git(pair.worktree, "rev-parse", "HEAD") == fixture.base
        assert not fixture.rr.journal.effect(pair.attempt_id, "worker-commit-candidate")
        assert pair.database.exists()
        assert fixture.checks.calls == 1
    finally:
        fixture.close()


def test_detached_hook_helper_cannot_prove_host_closure(tmp_path):
    fixture = production_fixture(tmp_path / "detached")
    marker, pidfile = tmp_path / "finished", tmp_path / "helper-pid"
    program = (
        "import time; from pathlib import Path; time.sleep(2); Path("
        + repr(str(marker)) + ").write_text('finished')"
    )
    hook = fixture.rr.repo / ".git/hooks/pre-commit"
    hook.write_text(
        "#!" + sys.executable + "\nimport subprocess, sys\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-I', '-c', " + repr(program)
        + "], start_new_session=True, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "Path(" + repr(str(pidfile)) + ").write_text(str(child.pid))\n"
    )
    hook.chmod(0o700)
    try:
        result = fixture.driver.run(fixture.ref)
        row = fixture.store.read(fixture.ref, fixture.rig.scope)
        pair = fixture.runtime.pair(row["attempt_id"])
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        assert os.getpgid(pid) == pid
        assert result["state"] == "uncertain", result
        assert not marker.exists()
        assert pair.database.exists()
        records = fixture.rr.journal.observations(pair.attempt_id, "worker-git-result")
        commit = [record for record in records if record["stage"] == "commit"][-1]
        assert commit["lifetime"]["confirmed_stop"] is False
        assert not fixture.rr.journal.observations(pair.attempt_id, "worker-commit-complete")
        with pytest.raises(RuntimeRefused, match="Unresolved worker commit"):
            fixture.rr.resources.cleanup(fixture.rig.core, fixture.rig.handle, pair.attempt_id)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "Bounded helper must finish before fixture cleanup"
        assert fixture.driver.run(fixture.ref)["state"] == "uncertain"
        restarted = WorkerGit(fixture.rr.journal)
        with pytest.raises(RuntimeUncertain, match="containment"):
            restarted.require_quiescent_commit(pair.attempt_id)
    finally:
        deadline = time.monotonic() + 5
        while pidfile.exists() and not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        fixture.close()


def test_missing_host_result_cannot_reconcile_group_absence(tmp_path):
    fixture = production_fixture(tmp_path / "missing-result")
    observe = fixture.rr.journal.observe

    def lose(attempt_id, kind, data):
        if kind == "worker-git-result" and data["stage"] == "commit":
            raise RuntimeError("Lost result after Git exited")
        return observe(attempt_id, kind, data)

    fixture.rr.journal.observe = lose
    try:
        assert fixture.driver.run(fixture.ref)["state"] == "blocked"
        fixture.rr.journal.observe = observe
        row = fixture.store.read(fixture.ref, fixture.rig.scope)
        pair = fixture.runtime.pair(row["attempt_id"])
        assert git(pair.worktree, "rev-parse", "HEAD") != fixture.base
        assert fixture.driver.run(fixture.ref)["state"] == "uncertain"
        assert pair.database.exists() and fixture.checks.calls == 1
        assert not fixture.rr.journal.observations(pair.attempt_id, "worker-commit-complete")
    finally:
        fixture.rr.journal.observe = observe
        fixture.close()


def test_shared_index_writer_after_validation_cannot_enter_private_commit(tmp_path):
    fixture = production_fixture(tmp_path / "late-writer")
    commit = fixture.git.commit

    def interleave(attempt_id, path, *args, **kwargs):
        (path / "foreign.txt").write_text("Unapproved shared-index content.\n")
        git(path, "add", "--", "foreign.txt")
        return commit(attempt_id, path, *args, **kwargs)

    fixture.git.commit = interleave
    try:
        assert fixture.driver.run(fixture.ref)["state"] == "uncertain"
        row = fixture.store.read(fixture.ref, fixture.rig.scope)
        pair = fixture.runtime.pair(row["attempt_id"])
        assert git(
            pair.worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        ).splitlines() == ["result.py"]
        assert pair.database.exists()
    finally:
        fixture.close()


def test_enabled_hook_cannot_replace_checked_tree_receipt(tmp_path):
    fixture = production_fixture(tmp_path / "hook-writer")
    hook = fixture.rr.repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'unchecked\\n' > foreign.txt\ngit add -- foreign.txt\n")
    hook.chmod(0o700)
    try:
        assert fixture.driver.run(fixture.ref)["state"] == "uncertain"
        row = fixture.store.read(fixture.ref, fixture.rig.scope)
        pair = fixture.runtime.pair(row["attempt_id"])
        candidate = fixture.rr.journal.effect(pair.attempt_id, "worker-commit-candidate")
        assert "foreign.txt" in git(
            pair.worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        ).splitlines()
        with pytest.raises(RuntimeUncertain, match="differs from its saved intent"):
            fixture.git.committed(pair.worktree, candidate)
        assert not fixture.rr.journal.observations(pair.attempt_id, "worker-commit-complete")
        assert pair.database.exists()
    finally:
        fixture.close()
