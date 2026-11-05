"""Owned application databases cannot share a physical file with another path."""

import os

import pytest

from ai_task_manager.adapters.resources.sqlite import SQLiteFixtures
from ai_task_manager.supervisor.models import RuntimeRefused


def owned(tmp_path):
    provider = SQLiteFixtures()
    path = tmp_path / "owned.sqlite"
    return provider, path, provider.create(path, "fixture-attempt")


def test_database_alias_refuses_inspection_and_preserves_foreign_bytes(tmp_path):
    provider, path, _ = owned(tmp_path)
    foreign = tmp_path / "foreign.sqlite"
    os.link(path, foreign)
    before = foreign.read_bytes()
    with pytest.raises(RuntimeRefused, match="link"):
        provider.inspect(path, "fixture-attempt")
    assert path.exists() and foreign.read_bytes() == before


def test_database_alias_refuses_removal_without_claiming_cleanup(tmp_path):
    provider, path, receipt = owned(tmp_path)
    foreign = tmp_path / "foreign.sqlite"
    os.link(path, foreign)
    before = foreign.read_bytes()
    with pytest.raises(RuntimeRefused, match="link"):
        provider.remove(path, receipt)
    assert path.exists() and foreign.read_bytes() == before


def test_late_database_alias_is_rechecked_before_unlink(tmp_path, monkeypatch):
    provider, path, receipt = owned(tmp_path)
    foreign = tmp_path / "late-foreign.sqlite"
    before = path.read_bytes()
    inspect = provider.inspect

    def link_after_initial_inspection(path, owner):
        value = inspect(path, owner)
        if not foreign.exists():
            os.link(path, foreign)
        return value

    monkeypatch.setattr(provider, "inspect", link_after_initial_inspection)
    with pytest.raises(RuntimeRefused, match="link"):
        provider.remove(path, receipt)
    assert path.exists() and foreign.read_bytes() == before


def test_exclusively_owned_database_can_still_be_removed(tmp_path):
    provider, path, receipt = owned(tmp_path)
    provider.remove(path, receipt)
    assert not path.exists()


def test_manager_retains_both_resources_and_cleanup_claims_on_database_alias(rr):
    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    pair = dispatch.resources
    foreign = rr.path / "foreign-alias.sqlite"
    os.link(pair.database, foreign)
    before = foreign.read_bytes()
    with pytest.raises(RuntimeRefused, match="link"):
        rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
    assert pair.worktree.exists() and pair.database.exists()
    assert foreign.read_bytes() == before
    assert not rr.journal.observations(dispatch.attempt_id, "worktree_removed")
    assert not rr.journal.observations(dispatch.attempt_id, "database_removed")
