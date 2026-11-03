"""Scoped actual local Git/Core candidate controls; not full merge or host acceptance."""

from pathlib import Path

import pytest

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.domains.work.errors import WorkError
from tests.merge.candidates.helpers import CandidateRig


@pytest.fixture
def rig(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "owned")
        yield value
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_completed_worker_prepares_guarded_isolated_candidate(rig):
    before = (rig.tree / "task.txt").read_bytes()
    database = rig.pair.database.read_bytes()
    result = rig.service.prepare(rig.grant)
    assert result.status == "ready", result
    assert git(rig.tree, "rev-parse", "HEAD") == rig.source_head
    assert (
        rig.tree / "task.txt"
    ).read_bytes() == before and rig.pair.database.read_bytes() == database
    parents = git(Path(result.path), "rev-list", "--parents", "-n1", result.commit).split()
    assert parents == [result.commit, rig.target_head, rig.source_head]
    assert (rig.root / "guard.log").read_text().splitlines() == ["pre-commit", "commit-msg"]
    assert git(rig.tree, "rev-parse", "refs/heads/" + rig.target) == rig.target_head
    with pytest.raises(WorkError):
        rig.r.core.execution.validate_work_lease(rig.r.handle, rig.attempt.id, rig.attempt.version)
    assert rig.r.core.get_task(rig.r.handle, rig.task.id).observed_status == "In Progress"


def test_independent_stable_target_preserves_both_lineages(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "unrelated", target="stable", diverge=True)
        result = value.service.prepare(value.grant)
        assert result.status == "ready", result
        assert (Path(result.path) / "target.txt").read_text() == "target change\n"
        assert (Path(result.path) / "task.txt").read_text() == "worker committed change\n"
        assert git(value.tree, "rev-parse", "HEAD") == value.source_head
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_required_precommit_guard_is_not_bypassed(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "denied", deny="pre-commit")
        result = value.service.prepare(value.grant)
        assert result.status == "refused", result
        assert git(value.tree, "rev-parse", "refs/heads/dev") == value.target_head
        assert (value.root / "guard.log").read_text().splitlines() == ["pre-commit"]
        assert Path(result.path).exists()
        with pytest.raises(WorkError):
            value.service.cleanup(result.operation_ref)
    finally:
        if hasattr(value, "runtime"):
            value.close()
