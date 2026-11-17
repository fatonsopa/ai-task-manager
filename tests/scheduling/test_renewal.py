"""Actual Core transactions with explicitly authored renewal source controls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from ai_task_manager.domains.scheduling.models import Dependency, SchedulingBlocked, WorkRenewal
from ai_task_manager.domains.work.errors import WorkError
from tests.scheduling.helpers import snapshot


class AuthoredRenewals:
    def __init__(self, value):
        self.value = value
        self.callback = None

    def verify_renewal(self, reference):
        assert reference == "authored-renewal"
        if self.callback:
            self.callback()
        return self.value


def observation(rig, task, attempt, **changes):
    with rig.db.transaction() as conn:
        identity = rig.db.identity(conn)
    return replace(
        WorkRenewal(
            rig.scope,
            identity,
            task.session_id,
            task.id,
            task.version,
            task.decision_revision,
            attempt.id,
            attempt.version,
            attempt.worker_id,
            attempt.host,
            "a" * 64,
            1300,
            rig.now,
            rig.now + 30,
            snapshot(rig.core, rig.handle, task, rig.now),
        ),
        **changes,
    )


def renew(rig, source, key=None):
    rig.core.scheduling.renewals = source
    return rig.core.scheduling.renew(rig.handle, "authored-renewal", key or rig.key())


def hold(rig, task):
    current = rig.current(task)
    proof = rig.proof(
        "human_decision", task.id, {"previous_revision": current.decision_revision, "hold": True}
    )
    return rig.core.decisions.record(rig.handle, task.id, current.version, proof, rig.key())


def test_multiple_freshness_windows_preserve_attempt_and_execution_cap(rig):
    task, attempt = rig.running()
    for _ in range(4):
        rig.now += 20
        source = AuthoredRenewals(observation(rig, task, attempt))
        assert renew(rig, source)["state"] == "ready"
        assert (
            rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)
            == attempt
        )
    assert rig.now == 1080
    records = [
        e
        for e in rig.core.notifications.list_events(rig.handle)
        if e.kind == "scheduling_authority_renewed"
    ]
    assert len(records) == 4 and all(e.payload["execution_deadline"] == 1300 for e in records)
    assert rig.core.get_attempt(rig.handle, attempt.id) == attempt


@pytest.mark.parametrize(
    "variant",
    [
        "expired_prior",
        "expired_new",
        "stale_revision",
        "graph_changed",
        "priority_changed",
        "wrong_store",
        "wrong_scope",
        "wrong_task",
        "wrong_worker",
        "wrong_attempt_version",
        "human_hold",
        "terminal_attempt",
        "future_observation",
        "long_freshness",
        "missing_source",
    ],
)
def test_renewal_refuses_without_reviving_authority(rig, variant):
    task, attempt = rig.running()
    rig.now += 1
    value = observation(rig, task, attempt)
    if variant == "expired_prior":
        rig.now += 30
        value = observation(rig, task, attempt)
    elif variant == "expired_new":
        value = replace(value, expires_at=rig.now)
    elif variant == "stale_revision":
        value = replace(
            value,
            snapshot=replace(value.snapshot, source_revision=value.snapshot.source_revision - 1),
        )
    elif variant == "graph_changed":
        value = replace(
            value,
            snapshot=replace(
                value.snapshot, dependencies=(Dependency("external:unknown", "work_done"),)
            ),
        )
    elif variant == "priority_changed":
        value = replace(value, snapshot=replace(value.snapshot, priority="urgent"))
    elif variant == "wrong_store":
        value = replace(value, store_identity="another-store")
    elif variant == "wrong_scope":
        value = replace(value, scope=replace(value.scope, principal="another-principal"))
    elif variant == "wrong_task":
        other = rig.task()
        value = replace(value, task_id=other.id)
    elif variant == "wrong_worker":
        value = replace(value, worker_id="another-worker")
    elif variant == "wrong_attempt_version":
        value = replace(value, attempt_version=attempt.version + 1)
    elif variant == "human_hold":
        hold(rig, task)
    elif variant == "terminal_attempt":
        rig.settle(task, attempt)
    elif variant == "future_observation":
        value = replace(value, observed_at=rig.now + 1)
    elif variant == "long_freshness":
        value = replace(value, expires_at=rig.now + 31)
    source = AuthoredRenewals(value)
    with pytest.raises(WorkError):
        if variant == "missing_source":
            rig.core.scheduling.renew(rig.handle, "authored-renewal", rig.key())
        else:
            renew(rig, source)
    assert not any(
        e.kind == "scheduling_authority_renewed"
        for e in rig.core.notifications.list_events(rig.handle)
    )
    if variant in {"graph_changed", "priority_changed"}:
        with pytest.raises(SchedulingBlocked):
            rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)


def test_committed_replay_does_not_extend_receipt_or_allow_expired_work(rig):
    task, attempt = rig.running()
    rig.now += 1
    source = AuthoredRenewals(observation(rig, task, attempt))
    key = rig.key()
    first = renew(rig, source, key)
    rig.now += 31
    assert renew(rig, source, key) == first
    with pytest.raises(SchedulingBlocked):
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)


@pytest.mark.parametrize("first", ["hold", "renew"])
def test_independent_writers_serialize_hold_and_renewal(rig, first):
    task, attempt = rig.running()
    rig.now += 1
    source = AuthoredRenewals(observation(rig, task, attempt))
    if first == "hold":
        hold(rig, task)
        with pytest.raises(WorkError):
            renew(rig, source)
    else:
        entered, release, writer_started = Event(), Event(), Event()

        def paused():
            entered.set()
            assert release.wait(2)

        source.callback = paused
        with ThreadPoolExecutor(max_workers=2) as pool:
            renewal = pool.submit(renew, rig, source, "race-renew")
            assert entered.wait(2)

            def write_hold():
                writer_started.set()
                return hold(rig, task)

            human = pool.submit(write_hold)
            assert writer_started.wait(2)
            assert not human.done()
            release.set()
            assert renewal.result(timeout=3)["state"] == "ready"
            assert human.result(timeout=3).human_hold
    with pytest.raises(WorkError):
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)


def test_fresh_revision_at_same_clock_instant_never_extends_freshness_by_replay(rig):
    task, attempt = rig.running()
    source = AuthoredRenewals(observation(rig, task, attempt))
    first = renew(rig, source)
    assert first["expires_at"] == rig.now + 30
    with pytest.raises(SchedulingBlocked):
        renew(rig, source)
    assert rig.core.scheduling.explain(rig.handle, task.id)["expires_at"] == first["expires_at"]


def test_ordinary_renewal_retains_original_request_fingerprint_and_replay(rig):
    from ai_task_manager.storage.work import fingerprint

    task, attempt = rig.running()
    source = AuthoredRenewals(observation(rig, task, attempt))
    key = rig.key()
    first = renew(rig, source, key)
    with rig.db.transaction() as conn:
        recorded = conn.execute(
            "SELECT fingerprint FROM operations WHERE key=?", (key,)
        ).fetchone()[0]
    assert recorded == fingerprint({
        "command": "renew_work_authority", "observation_ref": "authored-renewal"
    })
    source.callback = lambda: pytest.fail("A recorded ordinary renewal must replay unchanged")
    assert renew(rig, source, key) == first
