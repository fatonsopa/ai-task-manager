"""Actual Core availability while publication stages an owned gated child."""

import json
import sqlite3
import threading
import time

import pytest
from test_composition import owned_chain


@pytest.mark.parametrize("stage", ["inventory", "journal"])
def test_core_reader_and_hold_are_not_blocked_by_staging(tmp_path, stage):
    with owned_chain(tmp_path / "chain") as chain:
        publisher = chain.consumer.service.publisher
        entered, resume = threading.Event(), threading.Event()
        read, held = threading.Event(), threading.Event()
        original = publisher._inventory
        observations, errors, publications = {}, [], []
        intercepted = False

        def inventory(pid):
            nonlocal intercepted
            if not intercepted:
                intercepted = True
                entered.set()
                if not resume.wait(3):
                    raise AssertionError("Owned staging barrier expired")
            return original(pid)

        def publish():
            try:
                publications.append(chain.publish())
            except BaseException as error:
                errors.append({"thread": "publisher", "error": type(error).__name__})

        def hold():
            try:
                chain.r.core.get_attempt(chain.r.handle, chain.attempt.id)
                read.set()
                chain.runtime.hold(chain.dispatch)
                held.set()
            except BaseException as error:
                errors.append({"thread": "hold", "error": type(error).__name__})

        publisher._inventory = inventory
        worker = threading.Thread(target=publish, name="owned-publication")
        holder = threading.Thread(target=hold, name="owned-core-hold")
        lock = None
        worker.start()
        try:
            assert entered.wait(3), "Publisher did not reach the actual inventory boundary"
            if stage == "journal":
                lock = sqlite3.connect(chain.journal.path, timeout=0, isolation_level=None)
                lock.execute("BEGIN IMMEDIATE")
                resume.set()
            start = time.monotonic()
            holder.start()
            observations = {
                "stage": stage,
                "reader_completed_before_unblock": read.wait(0.3),
                "human_hold_completed_before_unblock": held.wait(0.3),
                "observation_seconds": time.monotonic() - start,
                "journal_writer_held": lock is not None,
            }
        finally:
            if lock is not None:
                lock.rollback()
                lock.close()
            resume.set()
            worker.join(10)
            if holder.ident is not None:
                holder.join(10)
            observations.update(
                worker_stopped=not worker.is_alive(),
                hold_thread_stopped=not holder.is_alive(),
                errors=errors,
                publications=publications,
            )
            (chain.runtime.path / "lock-observation.json").write_text(
                json.dumps(observations, indent=2)
            )
        assert not worker.is_alive() and not holder.is_alive()
        assert not errors
        assert observations["reader_completed_before_unblock"]
        assert observations["human_hold_completed_before_unblock"]
        assert publications[0]["publication"] != "committed"
        assert chain.target() == chain.old
