"""Interrupted launch ownership and the final short Core authority boundary."""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest
from f1_fixtures import FaultOnce
from test_composition import owned_chain

from ai_task_manager.adapters.git.publication import LocalGit


@pytest.mark.parametrize("stage", ["before_child_spawn", "after_child_spawn", "after_child_owned"])
def test_interrupted_staging_retains_generation_and_never_relaunches(tmp_path, stage):
    with owned_chain(tmp_path / "chain") as chain:
        publisher = chain.consumer.service.publisher
        publisher.faults = FaultOnce(stage)
        result = chain.publish()
        data = chain.journal.current(chain.consumer.registration["ref"])["facts"]["local_git"]
        assert len(data["spawn_intents"]) == 1 and chain.target() == chain.old
        if stage == "before_child_spawn":
            assert result["publication"] == "uncertain" and not result["quiescent"]
            assert not data.get("children")
        else:
            assert result["publication"] == "refused" and result["quiescent"]
            assert result["process_observation"]["returncode"] == 97
            assert data["children"][0]["generation"] == data["spawn_intents"][0]["generation"]
        children = data.get("children", [])
        repeated = chain.publish()
        assert repeated["publication"] == result["publication"]
        assert (
            chain.journal.current(chain.consumer.registration["ref"])["facts"]["local_git"].get(
                "children", []
            )
            == children
        )
        assert not publisher._live


@pytest.mark.parametrize("change", ["hold", "revoke", "candidate", "input"])
def test_authority_drift_after_ownership_prevents_actual_release(tmp_path, change):
    from ai_task_manager.adapters.resources.git import git

    with owned_chain(tmp_path / "chain") as chain:

        def drift():
            if change == "hold":
                chain.runtime.hold(chain.dispatch)
            elif change == "revoke":
                chain.r.core.revoke_session(chain.r.handle)
            elif change == "candidate":
                git(chain.tree, "update-ref", chain.request.source_ref, chain.old, chain.new)
            else:
                (chain.tree / "src/a.txt").write_text("changed after checks")

        publisher = chain.consumer.service.publisher
        publisher.faults = FaultOnce("after_child_owned", drift)
        result = chain.publish()
        assert result["publication"] == "refused" and result["quiescent"]
        assert result["process_observation"]["returncode"] == 97
        assert chain.target() == chain.old and not publisher._live


def test_missing_launch_observation_stays_uncertain_until_original_owner_recovers(tmp_path):
    with owned_chain(tmp_path / "chain") as chain:
        original = chain.consumer.service.publisher

        class Interrupted:
            def hit(self, stage):
                if stage == "after_child_spawn":
                    raise KeyboardInterrupt("Authored interrupted controller")

        original.faults = Interrupted()
        with pytest.raises(KeyboardInterrupt):
            chain.publish()
        ref = chain.consumer.registration["ref"]
        fresh = LocalGit(chain.journal, chain.profile, chain.consumer.service)

        def forbid(*args, **kwargs):
            raise AssertionError("Uncertain launch must not be repeated")

        fresh._spawn = forbid
        try:
            result = fresh.publish(
                ref, chain.journal.current(ref)["version"], "retry", chain.request
            )
            assert result["publication"] == "uncertain" and not result["quiescent"]
            assert not chain.journal.current(ref)["facts"]["local_git"].get("children")
            assert chain.target() == chain.old and not fresh._live
        finally:
            recovered = original.reconcile(ref)
        assert recovered["publication"] == "refused" and recovered["quiescent"]
        assert not original._live


def test_actual_core_yield_excludes_preparation_and_journal_io(tmp_path, monkeypatch):
    import ai_task_manager.adapters.git.publication as module

    with owned_chain(tmp_path / "chain") as chain:
        active, violations, durations = False, [], []
        boundary = chain.r.core.execution.work_snapshot_boundary

        @contextmanager
        def observed(*args):
            nonlocal active
            with boundary(*args) as snapshot:
                active = True
                started = time.monotonic()
                try:
                    yield snapshot
                finally:
                    durations.append(time.monotonic() - started)
                    active = False

        monkeypatch.setattr(chain.r.core.execution, "work_snapshot_boundary", observed)
        for obj, name in [
            (module.subprocess, "Popen"),
            (chain.consumer.service.publisher, "_inventory"),
            (chain.journal, "current"),
            (chain.journal, "commit"),
        ]:
            actual = getattr(obj, name)

            def checked(*args, _actual=actual, _name=name, **kwargs):
                if active:
                    violations.append(_name)
                return _actual(*args, **kwargs)

            monkeypatch.setattr(obj, name, checked)
        assert chain.publish()["publication"] == "committed"
        assert durations and not violations
        (chain.runtime.path / "short-boundary-observation.json").write_text(
            json.dumps(
                {
                    "durations_seconds": durations,
                    "forbidden_calls_inside": violations,
                    "scope": "Actual local pipe-release Core yield; no universal latency claim",
                },
                indent=2,
            )
        )


def test_post_release_journal_contention_keeps_core_available(tmp_path):
    with owned_chain(tmp_path / "chain") as chain:
        publisher = chain.consumer.service.publisher
        entered, resume, held = threading.Event(), threading.Event(), threading.Event()
        original, results, errors = publisher._write, [], []

        def write(ref, data, stage):
            if stage == "released":
                entered.set()
                assert resume.wait(3)
            return original(ref, data, stage)

        publisher._write = write

        def publish():
            try:
                results.append(chain.publish())
            except BaseException as error:
                errors.append(type(error).__name__)

        def hold():
            try:
                chain.r.core.get_attempt(chain.r.handle, chain.attempt.id)
                chain.runtime.hold(chain.dispatch)
                held.set()
            except BaseException as error:
                errors.append(type(error).__name__)

        worker, holder = threading.Thread(target=publish), threading.Thread(target=hold)
        lock = None
        worker.start()
        try:
            assert entered.wait(3)
            lock = sqlite3.connect(chain.journal.path, timeout=0, isolation_level=None)
            lock.execute("BEGIN IMMEDIATE")
            resume.set()
            holder.start()
            available = held.wait(0.5)
        finally:
            if lock:
                lock.rollback()
                lock.close()
            resume.set()
            worker.join(10)
            if holder.ident is not None:
                holder.join(10)
        assert not worker.is_alive() and not holder.is_alive() and not errors
        assert available
        assert results[0]["publication"] == "committed" and chain.target() == chain.new
