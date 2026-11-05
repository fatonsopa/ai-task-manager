"""Physical resources survive every declared consumer and verify on reacquisition."""

import multiprocessing
import sqlite3

import pytest

from ai_task_manager.supervisor.models import RuntimeRefused


def stopped(rr):
    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    return dispatch


def cleanup(rr, dispatch):
    return rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)


def test_last_consumer_retains_both_resources(rr):
    dispatch = stopped(rr)
    pair = dispatch.resources
    with rr.resources.use(pair):
        with rr.resources.use(pair):
            with pytest.raises(RuntimeRefused, match="consumer"):
                cleanup(rr, dispatch)
        with pytest.raises(RuntimeRefused, match="consumer"):
            cleanup(rr, dispatch)
        assert pair.worktree.exists() and pair.database.exists()
        assert not rr.journal.observations(dispatch.attempt_id, "database_removed")
    assert cleanup(rr, dispatch) == {"worktree": "removed", "database": "removed"}


def test_reacquisition_verifies_database_readiness(rr):
    pair = stopped(rr).resources
    with rr.resources.use(pair) as first:
        with sqlite3.connect(pair.database) as db:
            assert db.execute("SELECT attempt_id FROM fixture_owner").fetchall() == [
                (pair.attempt_id,)
            ]
    with rr.resources.use(pair) as second:
        assert second == first


@pytest.mark.parametrize("replacement", [False, True])
def test_missing_or_replaced_database_refuses_reacquisition(rr, replacement):
    pair = stopped(rr).resources
    with rr.resources.use(pair):
        pass
    retained = pair.database.with_suffix(".retained")
    pair.database.rename(retained)
    if replacement:
        pair.database.write_bytes(b"foreign database sentinel")
    with pytest.raises(RuntimeRefused):
        with rr.resources.use(pair):
            pytest.fail("Changed resource must not be admitted")
    assert retained.exists() and pair.worktree.exists()
    if replacement:
        assert pair.database.read_bytes() == b"foreign database sentinel"
    else:
        assert not pair.database.exists()


def hold_consumer(resources, pair, ready, release):
    with resources.use(pair):
        ready.set()
        release.wait(5)


def test_consumer_lock_is_process_shared_and_bounded(rr):
    dispatch = stopped(rr)
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(
        target=hold_consumer, args=(rr.resources, dispatch.resources, ready, release)
    )
    child.start()
    try:
        assert ready.wait(5)
        with pytest.raises(RuntimeRefused, match="consumer"):
            cleanup(rr, dispatch)
        assert dispatch.resources.database.exists()
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert child.exitcode == 0
    cleanup(rr, dispatch)


def test_foreign_lock_bytes_are_preserved(rr):
    pair = stopped(rr).resources
    directory = rr.resources.root / "consumers"
    directory.mkdir(exist_ok=True)
    lock = directory / f"{pair.attempt_id}.lock"
    lock.write_bytes(b"foreign lock sentinel")
    lock.chmod(0o644)
    with pytest.raises(RuntimeRefused):
        with rr.resources.use(pair):
            pytest.fail("Foreign lock must not be admitted")
    assert lock.read_bytes() == b"foreign lock sentinel"
    assert pair.database.exists() and pair.worktree.exists()


def test_replaced_fence_cannot_bypass_live_consumer(rr):
    dispatch = stopped(rr)
    pair = dispatch.resources
    with rr.resources.use(pair):
        path = rr.resources.root / "consumers" / f"{pair.attempt_id}.lock"
        path.rename(path.with_suffix(".original"))
        path.touch(mode=0o600)
        with pytest.raises(RuntimeRefused, match="identity"):
            cleanup(rr, dispatch)
        assert pair.database.exists() and pair.worktree.exists()
    with pytest.raises(RuntimeRefused, match="identity"):
        with rr.resources.use(pair):
            pytest.fail("Replaced fence must not be admitted")


def test_cleanup_boundary_refuses_new_consumers(rr):
    pair = stopped(rr).resources
    with rr.resources._consumer_boundary(pair.attempt_id, exclusive=True):
        with pytest.raises(RuntimeRefused, match="consumer"):
            with rr.resources.use(pair):
                pytest.fail("Cleanup owns the exclusive boundary")
    with rr.resources.use(pair):
        assert pair.database.exists()


def test_pending_durable_wait_retains_policy_and_resumes_verified(rr):
    from ai_task_manager.adapters.resources.release import DeliveryReleasePolicy

    dispatch = stopped(rr)
    pair = dispatch.resources
    rr.resources.release_policy = DeliveryReleasePolicy(rr.repo)
    with rr.resources.use(pair):
        pass
    # No active reader is not delivery permission while the workflow is pending.
    with pytest.raises(RuntimeRefused, match="delivery"):
        cleanup(rr, dispatch)
    with rr.resources.use(pair):
        rr.resources.verify(pair)
        with sqlite3.connect(pair.database) as db:
            assert db.execute("SELECT attempt_id FROM fixture_owner").fetchall() == [
                (pair.attempt_id,)
            ]
    assert not rr.journal.observations(dispatch.attempt_id, "database_removed")


def test_unobserved_empty_fence_is_not_silently_adopted(rr):
    pair = stopped(rr).resources
    directory = rr.resources.root / "consumers"
    directory.mkdir()
    lock = directory / f"{pair.attempt_id}.lock"
    lock.touch(mode=0o600)
    before = lock.stat().st_ino
    with pytest.raises(RuntimeRefused, match="unconfirmed"):
        with rr.resources.use(pair):
            pytest.fail("Unknown fence must not be adopted")
    assert lock.stat().st_ino == before and lock.read_bytes() == b""
    assert pair.database.exists() and pair.worktree.exists()


def test_same_inode_changed_database_owner_refuses_resume(rr):
    pair = stopped(rr).resources
    with rr.resources.use(pair):
        pass
    before = pair.database.stat().st_ino
    with sqlite3.connect(pair.database) as db:
        db.execute("UPDATE fixture_owner SET attempt_id='foreign-owner'")
    data = pair.database.read_bytes()
    with pytest.raises(RuntimeRefused):
        with rr.resources.use(pair):
            pytest.fail("Wrong database owner must not be admitted")
    assert pair.database.stat().st_ino == before
    assert pair.database.read_bytes() == data and pair.worktree.exists()


def test_foreign_session_cleanup_cannot_create_consumer_metadata(rr):
    from dataclasses import replace

    from ai_task_manager.domains.work.errors import WorkError

    dispatch = stopped(rr)
    foreign = rr.rig.session(replace(rr.rig.scope, logical_session="foreign-session"))
    directory = rr.resources.root / "consumers"
    before = tuple(directory.iterdir()) if directory.exists() else ()
    observations = rr.journal.observations(dispatch.attempt_id, "consumer_fence")
    with pytest.raises(WorkError):
        rr.resources.cleanup(rr.rig.core, foreign, dispatch.attempt_id)
    assert (tuple(directory.iterdir()) if directory.exists() else ()) == before
    assert rr.journal.observations(dispatch.attempt_id, "consumer_fence") == observations
    assert dispatch.resources.worktree.exists() and dispatch.resources.database.exists()
