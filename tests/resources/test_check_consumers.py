"""Authored final-check consumers retain owned resources through target movement."""

import json
import sqlite3
import subprocess
import time

import pytest

from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.checks import CheckStore
from ai_task_manager.supervisor.models import RuntimeRefused
from tests.checks.fixtures import (
    SCOPE,
    FixtureExecutor,
    Guard,
    catalogs,
    change,
    discovery,
    sha,
    unit,
    write_inputs,
)


def test_target_wait_invalidates_static_proof_and_rechecks_owned_database(rr, record_property):
    dispatch = rr.start()
    rr.supervisor.stop(rr.rig.handle, dispatch, operation_key=rr.rig.key())
    pair = dispatch.resources
    root = pair.worktree
    units = (unit("a"), unit("b", "checks b"), unit("c", "checks c"))
    write_inputs(root, units)
    configured = catalogs(root)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    started = time.monotonic()
    git("add", "src", "tests", "config", "project-checks.json")
    git("commit", "-qm", "Authored dependency fixture")
    commit_seconds = time.monotonic() - started

    class OwnedGuard:
        def verify(self, plan):
            rr.resources.verify(pair)
            current = git("rev-parse", "HEAD")
            if plan.head != current or plan.source_digest != sha(current):
                raise AccessDenied("Target moved; old static check plan is invalid")
            Guard(source=sha(current), root=root).verify(plan)

    executor = FixtureExecutor(root)
    checks = MergeChecks(
        CheckStore(rr.path / "consumer-checks.sqlite"), OwnedGuard(), executor, catalogs=configured
    )
    timings, results = {}, {}

    def run(label, selected, paths):
        head = git("rev-parse", "HEAD")
        ref = checks.prepare(
            SCOPE, label, sha(head), change(paths, head=head), discovery(selected), label
        )
        before = time.monotonic()
        result = checks.run(ref, SCOPE)
        timings[label] = time.monotonic() - before
        assert result["outcome"] == "passed"
        results[label] = {
            "physical": sum(row["result"]["physical_count"] for row in result["groups"].values()),
            "carried": sum(row["result"]["carried_count"] for row in result["groups"].values()),
        }
        return ref

    with rr.resources.use(pair):
        stale = run("small-cold", units[:1], ("src/a.txt",))
        run("small-warm", units[:1], ("src/a.txt",))
        assert len(executor.records) == 1
        before = time.monotonic()
        time.sleep(0.02)  # Frozen controlled wait; no performance target inferred.
        timings["wait"] = time.monotonic() - before
        before = time.monotonic()
        git("commit", "--allow-empty", "-qm", "Authored target transition")
        timings["target_commit"] = time.monotonic() - before
        with pytest.raises(RuntimeRefused, match="consumer"):
            rr.resources.cleanup(rr.rig.core, rr.rig.handle, dispatch.attempt_id)
        with pytest.raises(AccessDenied, match="Target moved"):
            checks.run(stale, SCOPE)
        assert len(executor.records) == 1
        before = time.monotonic()
        rr.resources.verify(pair)  # Required fresh readiness after waiting.
        with sqlite3.connect(pair.database) as db:
            assert db.execute("SELECT attempt_id FROM fixture_owner").fetchall() == [
                (pair.attempt_id,)
            ]
        timings["database_after_wait"] = time.monotonic() - before
        run("broad-cold", units, ("config/checks.json",))
        run("broad-warm", units, ("config/checks.json",))
        assert len(executor.records) == 4
    with rr.resources.use(pair):
        rr.resources.verify(pair)
        assert pair.database.exists()
    assert results == {
        "small-cold": {"physical": 1, "carried": 0},
        "small-warm": {"physical": 0, "carried": 1},
        "broad-cold": {"physical": 3, "carried": 0},
        "broad-warm": {"physical": 0, "carried": 3},
    }
    for record in executor.records:
        assert record["completion"]["group"]["confirmed_stop"]
    record_property(
        "authored_timing",
        json.dumps(
            {
                "seconds": {**timings, "fixture_commit": commit_seconds, "publication": None},
                "results": results,
                "actual_processes": len(executor.records),
                "scope": "authored one-unit/three-group cases; no workload benchmark",
            }
        ),
    )
