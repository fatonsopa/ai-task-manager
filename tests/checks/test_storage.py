import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ai_task_manager.domains.work.errors import (
    AccessDenied,
    IdempotencyConflict,
    SchemaError,
    StaleVersion,
)
from ai_task_manager.storage.checks import CheckStore
from tests.checks.fixtures import SCOPE, change, execution, plan, private_dir


def store(tmp_path, **kwargs):
    return CheckStore(private_dir(tmp_path) / "checks.sqlite", **kwargs)


@pytest.mark.parametrize(
    "stage", ["plan_before_commit", "dispatch_before_commit", "result_before_commit"]
)
def test_atomic_faults(tmp_path, stage):
    class Fault:
        def hit(self, actual):
            if actual == stage:
                raise RuntimeError("Authored transaction fault")

    db, p = store(tmp_path), plan()
    if stage == "plan_before_commit":
        db.faults = Fault()
        with pytest.raises(RuntimeError):
            db.create(p, "key")
        db.faults = None
        ref = db.create(p, "key")
        assert len(db.read(ref, SCOPE)["events"]) == 1
        return
    ref = db.create(p, "key")
    if stage == "result_before_commit":
        db.begin(ref, SCOPE, "checks a", 0, ("a",), {})
    before = db.read(ref, SCOPE)
    db.faults = Fault()
    with pytest.raises(RuntimeError):
        if stage == "dispatch_before_commit":
            db.begin(ref, SCOPE, "checks a", 0, ("a",), {})
        else:
            db.finish(ref, SCOPE, "checks a", 1, execution(p))
    assert db.read(ref, SCOPE)["events"] == before["events"]
    assert db.proofs(p, p.units[0]) == []


def test_append_only_attempts(tmp_path):
    db, p = store(tmp_path), plan()
    first = db.create(p, "key")
    assert db.create(p, "key") == first
    with pytest.raises(IdempotencyConflict):
        db.create(replace(p, head="3" * 40), "key")
    db.begin(first, SCOPE, "checks a", 0, ("a",), {})
    db.finish(first, SCOPE, "checks a", 1, execution(p))
    before = db.read(first, SCOPE)
    retry = db.create(replace(p, attempt="retry"), "retry")
    assert retry != first and db.read(first, SCOPE) == before


def test_plan_binds_discovery_and_change(tmp_path):
    db, original = store(tmp_path), plan()
    ref = db.create(original, "key")
    readback = db.read(ref, SCOPE)["plan"]
    assert readback.change_paths == ("src/a.txt",)
    assert readback.inventory_size == 1 and len(readback.inventory_digest) == 64
    assert readback.selection_availability == "known_selected"
    changed = plan(changes=change(("src/a.txt", "unrelated.txt")))
    assert changed.units == original.units and changed.digest != original.digest
    with pytest.raises(IdempotencyConflict):
        db.create(changed, "key")


@pytest.mark.parametrize(
    "mutation",
    [
        "format_bool",
        "missing_fact",
        "empty_groups",
        "head_bool",
        "inventory_bool",
        "binding_digest",
        "required_type",
        "input_kind",
        "labels_empty",
    ],
)
def test_malformed_plan_is_not_repaired(tmp_path, mutation):
    db, original = store(tmp_path), plan()
    ref = db.create(original, "key")
    with sqlite3.connect(db.path) as connection:
        body = json.loads(connection.execute("SELECT body FROM check_plans").fetchone()[0])
        if mutation == "format_bool":
            body["format"] = True
        elif mutation == "missing_fact":
            del body["inventory_digest"]
        elif mutation == "empty_groups":
            body["groups"] = []
        elif mutation == "head_bool":
            body["head"] = True
        elif mutation == "binding_digest":
            del body["binding_digest"]
        elif mutation == "required_type":
            body["groups"][0]["required"] = 1
        elif mutation == "input_kind":
            body["groups"][0]["input_kinds"] = ["invented"]
        elif mutation == "labels_empty":
            body["groups"][0]["labels"] = []
        else:
            body["inventory_size"] = True
        from ai_task_manager.domains.checks.models import fingerprint

        connection.execute(
            "UPDATE check_plans SET body=?,digest=?", (json.dumps(body), fingerprint(body))
        )
    with pytest.raises(SchemaError):
        db.read(ref, SCOPE)


def test_concurrent_result_writes(tmp_path):
    import time

    from ai_task_manager.application.merge_checks.read_retries import ReadRetryPolicy, RetryingReads

    db, p = store(tmp_path), plan()

    class Parent:
        deadline = time.monotonic() + 5

        def remaining_seconds(self):
            return self.deadline - time.monotonic()

    reads = RetryingReads(db, ReadRetryPolicy(50, 1, 0.005), Parent())

    def write(number):
        ref = db.create(replace(p, attempt=str(number)), str(number))
        own = reads.read(ref, SCOPE, after_wait=lambda: None)["plan"]
        db.begin(ref, SCOPE, "checks a", 0, ("a",), {})
        db.finish(ref, SCOPE, "checks a", 1, execution(own))
        return ref

    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(write, range(4)))
    assert len(set(refs)) == 4
    assert all(
        reads.read(ref, SCOPE, after_wait=lambda: None)["outcome"] == "passed" for ref in refs
    )
    assert len(reads.proofs(p, p.units[0], after_wait=lambda: None)) == 4


def test_read_preserves_history_after_writer_release_v2(tmp_path):
    # Version 1 immediate-read success is explicitly superseded by read_contract_v2.json.
    import time
    from threading import Event

    from ai_task_manager.application.merge_checks.read_retries import ReadRetryPolicy, RetryingReads

    db, p = store(tmp_path), plan()
    ref = db.create(p, "key")
    before = db.read(ref, SCOPE)
    locked, release = Event(), Event()

    def writer():
        with db._transaction() as connection:
            connection.execute("UPDATE check_meta SET version=version")
            locked.set()
            assert release.wait(timeout=1)

    class Parent:
        deadline = time.monotonic() + 1

        def remaining_seconds(self):
            return self.deadline - time.monotonic()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        assert locked.wait(timeout=1)

        def wait(seconds):
            release.set()
            future.result(timeout=1)

        reader = RetryingReads(db, ReadRetryPolicy(3, 0.5, 0.01), Parent(), sleep=wait)
        try:
            assert reader.read(ref, SCOPE, after_wait=lambda: None) == before
        finally:
            release.set()
        future.result(timeout=1)
    assert db.read(ref, SCOPE) == before


@pytest.mark.parametrize("mutation", ["inode", "store_id"])
def test_store_identity_changed(tmp_path, mutation):
    db, p = store(tmp_path), plan()
    ref = db.create(p, "key")
    if mutation == "inode":
        copied = tmp_path / "copied.sqlite"
        shutil.copyfile(db.path, copied)
        copied.chmod(0o600)
        copied.replace(db.path)
    else:
        with sqlite3.connect(db.path) as connection:
            connection.execute("UPDATE check_meta SET store_id='different'")
    with pytest.raises(AccessDenied):
        db.read(ref, SCOPE)


def test_unresolved_dispatch_is_not_repeated(tmp_path):
    db, p = store(tmp_path), plan()
    ref = db.create(p, "key")
    db.begin(ref, SCOPE, "checks a", 0, ("a",), {})
    reopened = CheckStore(db.path)
    assert reopened.read(ref, SCOPE)["outcome"] == "blocked"
    with pytest.raises(StaleVersion):
        reopened.begin(ref, SCOPE, "checks a", 0, ("a",), {})
    assert len(reopened.read(ref, SCOPE)["events"]) == 2


def test_proof_requires_original_physical_result(tmp_path):
    db, p = store(tmp_path), plan()
    ref = db.create(p, "key")
    db.begin(ref, SCOPE, "checks a", 0, ("a",), {})
    db.finish(ref, SCOPE, "checks a", 1, execution(p))
    assert len(db.proofs(p, p.units[0])) == 1
    with sqlite3.connect(db.path) as connection:
        value = json.loads(connection.execute("SELECT body FROM check_proofs").fetchone()[0])
        value["physical_origin"] = "result:absent"
        connection.execute("UPDATE check_proofs SET body=?", (json.dumps(value),))
    with pytest.raises(SchemaError):
        db.proofs(p, p.units[0])


def test_foreign_store_and_scope_refused(tmp_path):
    db, p = store(tmp_path), plan()
    ref = db.create(p, "key")
    with pytest.raises(AccessDenied):
        db.read(ref, replace(SCOPE, session="foreign"))
    other = tmp_path / "foreign.sqlite"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE tasks(secret TEXT)")
    other.chmod(0o600)
    with pytest.raises(SchemaError):
        CheckStore(other)
