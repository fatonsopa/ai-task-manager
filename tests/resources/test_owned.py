import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_task_manager.adapters.resources.paths import confined
from ai_task_manager.supervisor.models import RuntimeRefused, RuntimeUncertain


def test_real_git_and_sqlite_resources_are_separate_owned_fixtures(rr):
    dispatch = rr.start()
    pair = dispatch.resources
    fact = rr.resources.verify(pair)
    assert pair.worktree != rr.repo
    assert pair.database != rr.journal.path
    assert pair.database != Path(rr.rig.db.path)
    with sqlite3.connect(pair.database) as conn:
        assert conn.execute("SELECT attempt_id FROM fixture_owner").fetchall() == [
            (pair.attempt_id,)
        ]
    assert fact["worktree"]["common"] == str(rr.repo / ".git")


def test_resource_intent_is_committed_before_first_git_effect(rr, monkeypatch):
    task, attempt = rr.rig.reserved()
    rr.journal.claim(attempt, asdict(rr.rig.scope), deadline=rr.rig.now + 30)

    def fail(path, branch, base):
        assert rr.journal.effect(attempt.id, "resources")["worktree"] == str(path)
        raise OSError("controlled Git failure")

    monkeypatch.setattr(rr.resources.git, "create", fail)
    with pytest.raises(OSError):
        rr.resources.prepare(attempt.id)
    assert rr.journal.effect(attempt.id, "resources")


def test_partial_setup_is_retained_without_recreation(rr, monkeypatch):
    task, attempt = rr.rig.reserved()
    rr.journal.claim(attempt, asdict(rr.rig.scope), deadline=rr.rig.now + 30)

    def fail(*args):
        raise OSError("controlled DB failure")

    monkeypatch.setattr(rr.resources.databases, "create", fail)
    with pytest.raises(OSError):
        rr.resources.prepare(attempt.id)
    assert rr.journal.observations(attempt.id, "worktree_created")
    assert (rr.path / "owned/trees" / attempt.id).exists()
    with pytest.raises(RuntimeUncertain):
        rr.resources.reconcile(attempt.id)
    with pytest.raises(RuntimeUncertain):
        rr.resources.prepare(attempt.id)


def test_reconcile_existing_resources_uses_original_ownership(rr):
    dispatch = rr.start()
    recovered = rr.resources.reconcile(dispatch.attempt_id)
    assert recovered.worktree_id == dispatch.resources.worktree_id
    assert recovered.database_id == dispatch.resources.database_id


@pytest.mark.parametrize("relative", ["../escape", "/tmp/escape", ".", "nested/../../escape"])
def test_unsafe_paths_refuse_before_effect(tmp_path, relative):
    with pytest.raises(RuntimeRefused):
        confined(tmp_path, relative)


def test_symlink_resource_paths_and_roots_are_rejected(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (owned / "alias").symlink_to(foreign)
    with pytest.raises(RuntimeRefused):
        confined(owned, "alias/resource")
    with pytest.raises(RuntimeRefused):
        confined(owned / "alias", "resource")


def test_shared_or_metadata_databases_cannot_be_adopted(rr):
    for path in (rr.journal.path, Path(rr.rig.db.path)):
        with pytest.raises(RuntimeRefused):
            rr.resources.databases.create(path, "forged-owner")
    dispatch = rr.start()
    with pytest.raises(RuntimeRefused):
        rr.resources.databases.create(dispatch.resources.database, "other")
    with pytest.raises(RuntimeRefused):
        rr.resources.databases.inspect(dispatch.resources.database, "other")


@pytest.mark.parametrize("change", ["database_inode", "database_marker", "worktree_branch"])
def test_resource_identity_changes_prevent_release(rr, change):
    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    pair = dispatch.resources
    if change == "database_inode":
        pair.database.rename(pair.database.with_suffix(".old"))
        pair.database.touch()
    elif change == "database_marker":
        with sqlite3.connect(pair.database) as conn:
            conn.execute("UPDATE fixture_owner SET attempt_id='other'")
    else:
        from ai_task_manager.adapters.resources.git import git

        git(pair.worktree, "checkout", "-qb", "foreign")
    with pytest.raises(RuntimeRefused):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
    assert pair.worktree.exists() and pair.database.exists()


def test_active_and_unknown_attempts_keep_resources(rr, monkeypatch):
    dispatch = rr.start()
    with pytest.raises(RuntimeRefused):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
    from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop

    monkeypatch.setattr(
        dispatch.worker.group, "stop", lambda: GroupStop(False, GroupObservation(False))
    )
    result = rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    assert result.outcome == "unknown"
    with pytest.raises(RuntimeRefused):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
    monkeypatch.undo()


def test_dirty_tree_preserved_and_verified_cleanup_is_idempotent(rr):
    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    dirty = dispatch.resources.worktree / "unsaved.txt"
    dirty.write_text("retain")
    with pytest.raises(RuntimeRefused):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
    assert dispatch.resources.database.exists()
    dirty.unlink()
    assert (
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)["database"]
        == "removed"
    )
    assert not dispatch.resources.database.exists() and not dispatch.resources.worktree.exists()
    assert (
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)["worktree"]
        == "removed"
    )


def test_cleanup_keeps_other_task_and_foreign_database_unchanged(rr):
    first, other = rr.start(), rr.start()
    sentinel = rr.path / "foreign.sqlite"
    sentinel.write_bytes(b"foreign sentinel")
    before = other.resources.database.read_bytes()
    rr.supervisor.stop(rr.rig.handle, first, operation_key=rr.rig.key())
    rr.resources.cleanup(rr.rig.core, rr.rig.handle, first.attempt_id)
    assert other.resources.worktree.exists() and other.resources.database.read_bytes() == before
    assert sentinel.read_bytes() == b"foreign sentinel"


def test_default_release_policy_refuses_code_cleanup_before_delivery(rr):
    from ai_task_manager.adapters.resources.release import DeliveryReleasePolicy

    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    rr.resources.release_policy = DeliveryReleasePolicy(rr.repo)
    with pytest.raises(RuntimeRefused, match="delivery"):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
    assert dispatch.resources.worktree.exists() and dispatch.resources.database.exists()


def test_cancellation_does_not_discard_unmerged_commits(rr):
    from dataclasses import replace

    from ai_task_manager.adapters.resources.git import git
    from ai_task_manager.adapters.resources.release import DeliveryReleasePolicy

    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    git(dispatch.resources.worktree, "commit", "--allow-empty", "-qm", "Unmerged fixture")
    attempt = rr.rig.core.get_attempt(rr.rig.handle, dispatch.attempt_id)
    task = rr.rig.core.get_task(rr.rig.handle, attempt.task_id)

    class CanceledFixtureCore:
        def get_task(self, handle, task_id):
            return replace(task, observed_status="Canceled", sync_state="confirmed")

    fact = rr.journal.observations(attempt.id, "resources_ready")[-1]
    fact["worktree"]["head"] = git(dispatch.resources.worktree, "rev-parse", "HEAD")
    with pytest.raises(RuntimeRefused, match="Unmerged"):
        DeliveryReleasePolicy(rr.repo).authorize(
            CanceledFixtureCore(), rr.rig.handle, attempt, fact
        )
    assert dispatch.resources.worktree.exists()


def test_arbitrary_missing_resource_is_not_successful_cleanup(rr):
    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    dispatch.resources.database.unlink()
    with pytest.raises(RuntimeRefused):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
    with pytest.raises(RuntimeRefused):
        rr.resources.reconcile_removed(
            rr.rig.core, rr.rig.handle, dispatch.attempt_id, "database", "forged-removal:ref"
        )


def test_exact_trusted_removal_receipt_records_reconciled_tombstone(rr):
    from ai_task_manager.adapters.resources.release import RemovalReceipt

    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    dispatch.resources.database.unlink()

    class TrustedRemovalFixture:
        def verify_removal(self, ref):
            assert ref == "project-removal:fixture"
            return RemovalReceipt(
                dispatch.attempt_id,
                "database",
                dispatch.resources.database_id,
                "fixture-merge:receipt",
            )

    rr.resources.removal_verifier = TrustedRemovalFixture()
    identity = rr.resources.reconcile_removed(
        rr.rig.core, rr.rig.handle, dispatch.attempt_id, "database", "project-removal:fixture"
    )
    assert rr.journal.observation(identity)["source_ref"] == "fixture-merge:receipt"
    assert (
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)["database"]
        == "removed"
    )
