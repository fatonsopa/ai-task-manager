"""Version 2 retries only completed reads, with trusted time and attempt bounds."""

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from ai_task_manager.application.merge_checks.read_retries import (
    CheckReads,
    ReadRetryExhausted,
    ReadRetryPolicy,
    RetryingReads,
)
from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.storage.checks import CheckStore, CheckStoreBusy
from tests.checks.fixtures import (
    SCOPE,
    SOURCE,
    FixtureExecutor,
    Guard,
    RecordingExecutor,
    catalogs,
    change,
    discovery,
    private_dir,
    sha,
    unit,
    write_inputs,
)


class Parent:
    def __init__(self, seconds=1):
        self.deadline = time.monotonic() + seconds

    def remaining_seconds(self):
        return self.deadline - time.monotonic()


def policy(attempts=5):
    return ReadRetryPolicy(attempts, 0.5, 0.005)


@contextmanager
def held_writer(store):
    locked, release = threading.Event(), threading.Event()
    errors = []

    def writer():
        conn = sqlite3.connect(store.path, isolation_level=None, timeout=0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE check_meta SET version=version")
            locked.set()
            assert release.wait(timeout=2)
            conn.rollback()
        except BaseException as error:
            errors.append(error)
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert locked.wait(timeout=1)
        yield lambda: release.set()
    finally:
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive() and not errors


def rig(tmp_path, *, executor=None, guard=None):
    root = private_dir(tmp_path)
    store = CheckStore(root / "checks.sqlite")
    exec = executor or RecordingExecutor()
    service = MergeChecks(
        store,
        guard or Guard(),
        exec,
        catalogs=catalogs(root),
        reads=RetryingReads(store, policy(), Parent()),
    )
    ref = service.prepare(SCOPE, "attempt", SOURCE, change(), discovery(), "initial")
    return service, ref, exec


def test_retry_release_recovers_with_closed_failed_connections(tmp_path, monkeypatch):
    service, ref, executor = rig(tmp_path)
    store = service.store
    before = store.read(ref, SCOPE)
    opened = []
    actual = sqlite3.connect

    class Tracked(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def connect(*args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            kwargs["factory"] = Tracked
            conn = actual(*args, **kwargs)
            opened.append(conn)
            return conn
        return actual(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with held_writer(store) as release:
        waits = []

        def wait(seconds):
            assert opened and all(c.closed for c in opened)
            waits.append(seconds)
            release()
            time.sleep(seconds)

        service.reads.sleep = wait
        result = service.reads.read(ref, SCOPE, after_wait=lambda: service._verify(before["plan"]))
        assert result == before and waits
    assert store.read(ref, SCOPE) == before and executor.calls == []


def test_exhaustion_is_zero_effect_retryable_infrastructure(tmp_path):
    service, ref, executor = rig(tmp_path)
    before = service.store.read(ref, SCOPE)
    service.reads = RetryingReads(service.store, policy(2), Parent())
    with held_writer(service.store):
        with pytest.raises(ReadRetryExhausted) as raised:
            service.run(ref, SCOPE)
    assert raised.value.reason == "attempts" and raised.value.attempts == 2
    assert raised.value.retryable and not isinstance(raised.value, AccessDenied)
    assert executor.calls == [] and service.store.read(ref, SCOPE) == before


@pytest.mark.parametrize("which", ["source", "configuration"])
def test_after_wait_changes_refuse_before_effect(tmp_path, which):
    service, ref, executor = rig(tmp_path)
    before = service.store.read(ref, SCOPE)
    with held_writer(service.store) as release:

        def wait(seconds):
            if which == "source":
                service.source_guard.source = sha("changed")
            else:
                path = service.store.path.parent / "project-checks.json"
                path.write_text(path.read_text() + " ")
            release()
            time.sleep(seconds)

        service.reads.sleep = wait
        with pytest.raises(AccessDenied):
            service.run(ref, SCOPE)
    assert executor.calls == [] and service.store.read(ref, SCOPE) == before


@pytest.mark.parametrize("exhaust", [False, True])
def test_busy_after_committed_physical_result_never_repeats_executor(tmp_path, exhaust):
    root = private_dir(tmp_path)
    write_inputs(root, (unit(),))
    executor = FixtureExecutor(root)
    service, ref, _ = rig(root, executor=executor, guard=Guard(root=root))
    original_finish = service.store.finish
    holder = {}
    waits = []

    def finish(*args, **kwargs):
        result = original_finish(*args, **kwargs)
        holder["context"] = held_writer(service.store)
        holder["release"] = holder["context"].__enter__()
        return result

    service.store.finish = finish

    def wait(seconds):
        assert len(executor.records) == 1
        waits.append(seconds)
        if not exhaust:
            holder["release"]()
        time.sleep(seconds)

    service.reads = RetryingReads(service.store, policy(2), Parent(), sleep=wait)
    try:
        if exhaust:
            with pytest.raises(ReadRetryExhausted):
                service.run(ref, SCOPE)
        else:
            result = service.run(ref, SCOPE)
            assert result["outcome"] == "passed"
    finally:
        if "context" in holder:
            holder["context"].__exit__(None, None, None)
    assert waits and len(executor.records) == 1
    service.store.finish = original_finish
    replay = service.run(ref, SCOPE)
    assert replay["outcome"] == "passed" and len(executor.records) == 1
    assert replay["groups"]["checks a"]["result"]["physical_count"] == 1


def test_proof_read_contention_is_consumed_without_new_execution(tmp_path):
    service, first, executor = rig(tmp_path)
    service.run(first, SCOPE)
    ref = service.prepare(SCOPE, "retry", SOURCE, change(), discovery(), "retry")
    actual = service.store.proofs
    holder = {}

    def proofs(*args):
        if not holder:
            holder["context"] = held_writer(service.store)
            holder["release"] = holder["context"].__enter__()
        return actual(*args)

    service.store.proofs = proofs

    def wait(seconds):
        holder["release"]()
        time.sleep(seconds)

    service.reads.sleep = wait
    try:
        result = service.run(ref, SCOPE)
    finally:
        if holder:
            holder["context"].__exit__(None, None, None)
    assert result["groups"]["checks a"]["result"]["carried_count"] == 1
    assert len(executor.calls) == 1


@pytest.mark.parametrize("stage", ["begin", "finish", "executor"])
def test_no_automatic_retry_of_writes_or_physical_effects(tmp_path, stage):
    service, ref, executor = rig(tmp_path)
    effects = []
    if stage == "executor":

        def effect(*args):
            effects.append("effect")
            raise CheckStoreBusy("uncertain reply")

        executor.callback = effect
        result = service.run(ref, SCOPE)
        assert result["outcome"] == "blocked"
        assert effects == ["effect"]
        service.run(ref, SCOPE)
        assert effects == ["effect"]
    else:

        def busy(*args, **kwargs):
            effects.append(stage)
            raise CheckStoreBusy(stage)

        setattr(service.store, stage, busy)
        with pytest.raises(CheckStoreBusy):
            service.run(ref, SCOPE)
        assert effects == [stage]
        assert len(executor.calls) == (1 if stage == "finish" else 0)


@pytest.mark.parametrize("bound", ["attempts", "elapsed", "parent"])
def test_finite_bounds_and_no_post_deadline_attempt(bound):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    calls = []
    verified = []

    class Busy:
        def read(self, *args):
            calls.append(clock.now)
            raise CheckStoreBusy("busy")

    class Budget:
        def remaining_seconds(self):
            return (0.015 if bound == "parent" else 1) - clock.now

    p = ReadRetryPolicy(2 if bound == "attempts" else 10, 0.015 if bound == "elapsed" else 1, 0.01)
    reader = RetryingReads(Busy(), p, Budget(), clock=clock, sleep=clock.sleep)
    with pytest.raises(ReadRetryExhausted):
        reader.read("ref", SCOPE, after_wait=lambda: verified.append(clock.now))
    assert len(calls) <= 2
    assert not any(t >= 0.015 for t in calls) if bound != "attempts" else len(calls) == 2


@pytest.mark.parametrize(
    "values", [(True, 1, 0.1), (0, 1, 0.1), (2, float("inf"), 0.1), (2, 1, float("nan")), (2, 1, 0)]
)
def test_trusted_policy_rejects_unbounded_or_malformed(values):
    with pytest.raises(ValidationError):
        ReadRetryPolicy(*values)


def test_unavailable_parent_and_default_never_retry():
    calls = []

    class Busy:
        def read(self, *args):
            calls.append("read")
            raise CheckStoreBusy("busy")

    direct = CheckReads(Busy())
    with pytest.raises(CheckStoreBusy):
        direct.read("ref", SCOPE, after_wait=lambda: None)
    assert calls == ["read"]
    with pytest.raises(ValidationError):
        RetryingReads(Busy(), policy(), None)

    class ParentUnknown:
        def remaining_seconds(self):
            return float("inf")

    with pytest.raises(ValidationError):
        RetryingReads(Busy(), policy(), ParentUnknown()).read("ref", SCOPE, after_wait=lambda: None)
    assert calls == ["read"]


def test_original_oracle_is_retained_as_superseded_source():
    root = Path(__file__).parent / "frozen"
    data = json.loads((root / "read_contract_v2.json").read_text())
    old = (root / "read_under_writer_v1.txt").read_bytes()
    assert hashlib.sha256(old).hexdigest() == data["old_oracle"]["function_sha256"]
    assert "test_read_does_not_change_history" in old.decode()
    assert data["version"] == 2 and data["old_oracle"]["disposition"].startswith(
        "superseded explicitly"
    )


def test_read_commit_uncertainty_is_never_automatically_retried(tmp_path, monkeypatch):
    from ai_task_manager.storage.checks import CheckCommitBusy

    service, ref, _ = rig(tmp_path)
    actual = sqlite3.connect
    commits = []

    class CommitFailure(sqlite3.Connection):
        def commit(self):
            super().commit()
            commits.append("committed")
            error = sqlite3.OperationalError("fixture lost commit reply")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    def connect(*args, **kwargs):
        kwargs["factory"] = CommitFailure
        return actual(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(CheckCommitBusy):
        service.reads.read(ref, SCOPE, after_wait=lambda: None)
    assert commits == ["committed"]
