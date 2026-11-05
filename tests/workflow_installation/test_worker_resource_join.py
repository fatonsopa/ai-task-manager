"""Actual Core/owned resources prove installed commit fences; method cuts are authored."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.domains.work.errors import WorkError
from ai_task_manager.installation.workflows.worker_resources import ResourceWorkerCommits
from ai_task_manager.supervisor.models import RuntimeRefused
from tests.worker_commits.helpers import WorkerFixture


@pytest.fixture
def owned(tmp_path):
    value = WorkerFixture((tmp_path / "worker").resolve())
    try:
        row = value.execute_only()
        yield value, row
    finally:
        value.close()


@pytest.mark.parametrize("operation", ["prepare", "run", "reconcile"])
def test_worker_commit_consumers_hold_physical_fence(owned, monkeypatch, operation):
    fixture, row = owned
    pair = fixture.runtime.pair(row["attempt_id"])

    def observe(*args):
        with pytest.raises(RuntimeRefused, match="consumer"):
            fixture.rr.resources.cleanup(fixture.rig.core, fixture.rig.handle, pair.attempt_id)
        return {"authored_method_cut": operation}

    monkeypatch.setattr(fixture.commits, operation, observe)
    guarded = ResourceWorkerCommits(fixture.commits, fixture.rr.resources)
    method = getattr(guarded, operation)
    args = (row, fixture.plan) if operation == "prepare" else (row, fixture.plan, {})
    assert method(*args) == {"authored_method_cut": operation}
    assert pair.database.exists() and pair.worktree.exists()


def test_foreign_worker_commit_scope_refuses_before_fence_creation(owned):
    fixture, row = owned
    guarded = ResourceWorkerCommits(fixture.commits, fixture.rr.resources)
    directory = fixture.rr.resources.root / "consumers"
    before = tuple(directory.iterdir()) if directory.exists() else ()
    foreign = fixture.rig.session(replace(fixture.rig.scope, logical_session="foreign"))
    original = fixture.commits.handle
    fixture.commits.handle = foreign
    try:
        with pytest.raises(WorkError):
            guarded.prepare(row, fixture.plan)
    finally:
        fixture.commits.handle = original
    assert (tuple(directory.iterdir()) if directory.exists() else ()) == before
    assert not fixture.rr.journal.observations(row["attempt_id"], "consumer_fence")


@pytest.mark.parametrize("stage", ["execute", "result"])
def test_check_wait_resource_drift_refuses_before_returned_evidence(owned, stage):
    fixture, row = owned
    pair = fixture.runtime.pair(row["attempt_id"])
    original = pair.database.with_suffix(".retained")

    def change(*args, **kwargs):
        pair.database.rename(original)
        pair.database.write_bytes(b"authored replacement")
        pair.database.chmod(0o600)
        return {"unusable": True}

    checks = SimpleNamespace(execute_registered=change, verified_result=change)
    fixture.commits.checks = checks
    guarded = ResourceWorkerCommits(fixture.commits, fixture.rr.resources)
    try:
        with fixture.rr.resources.use(pair):
            with pytest.raises(RuntimeRefused):
                if stage == "execute":
                    guarded.checks.execute_registered(
                        "verify", pair, lambda: None, lambda: None, "key"
                    )
                else:
                    guarded.checks.verified_result(pair.attempt_id, "verify", operation_key="key")
    finally:
        if original.exists():
            pair.database.unlink()
            original.rename(pair.database)
