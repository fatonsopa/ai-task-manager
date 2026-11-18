"""Actual shared capture/admission consumers; authored protocol, no live models."""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Event

import pytest

from ai_task_manager.adapters.hosts.bridge.conformance import (
    ConformanceCapture,
    cases,
    prepare_profile,
)
from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native.admissions import NormalAdmissions
from ai_task_manager.installation.native.enrollment import CaptureEnrollments
from ai_task_manager.installation.native.store import NativeStore
from tests.hosts.conformance.test_capture import rig as rig
from tests.hosts.conformance.test_role_profile import RoleFactory
from tests.native_installation.test_identity_lifecycle import busy_event
from tests.native_installation.test_normal import actual_accounting_graph as actual_accounting_graph


@pytest.fixture
def shared(actual_accounting_graph, rig, tmp_path):
    authority, policy, binding, journal, store, manifest = actual_accounting_graph
    accounting = authority.launch_permits
    # Produce the real opaque CompletedCapture from authored protocol observations.
    rig.manifest = manifest
    rig.profile = prepare_profile(manifest, rig.pins, "2.1.301")
    rig.context = replace(
        rig.context,
        installation_digest=manifest.digest,
        profile_digest=rig.profile.fingerprint,
        account_hash=manifest.data["account_hash"],
    )
    rig.authority.context = rig.context
    rig.cases = cases(rig.context)
    (rig.root / "input.txt").unlink()
    producer = ConformanceCapture(
        manifest, rig.pins, rig.context, rig.authority, rig.journal, profile=rig.profile
    )
    reference = producer.run(RoleFactory(rig))
    capture_store = NativeStore.create(tmp_path / "shared-capabilities.sqlite")
    enrollments = CaptureEnrollments(capture_store)
    enrollments.enroll(
        producer.completed(), rig.journal, rig.pins, valid_until=accounting.plan.deadline
    )
    consumer = enrollments.consumer(manifest, reference)
    old = accounting.admissions
    admission = NormalAdmissions(
        manifest, old.store, old.permission, old.plan, old.controller, conformance=consumer
    )
    accounting.admissions = admission
    accounting._bound = (*accounting._bound[:-1], admission)
    return accounting, policy, binding, capture_store


def ready(accounting, policy, binding):
    initialization = accounting.authorize_initialization(policy, binding)
    for permit in (initialization.native, initialization.relay):
        with permit.launch_boundary() as report:
            report(process_pid=None, process_start_identity=None)
    journal = accounting.journal
    group = {"confirmed_stop": True, "observation": {"known": True, "live_pids": []}}
    auth = journal._append(
        "trace",
        {
            "auth_method": "claude.ai",
            "account_hash": accounting.admissions.manifest.data["account_hash"],
            "configuration_digest": binding.configuration_digest,
            "owned_group": group,
        },
    )
    native = journal._append(
        "trace",
        {
            "attempt_id": binding.attempt_id,
            "worker_id": binding.worker_id,
            "configuration_digest": binding.configuration_digest,
            "process": {"pid": 123456, "start_identity": "authored"},
            "frame": {"type": "system", "subtype": "init", "apiKeySource": "none"},
        },
    )
    journal._append(
        "startup",
        {**asdict(binding), "process_id": 123456, "process_start_identity": "authored"},
        sources=(auth, native),
        expires_at=journal.clock() + 30,
    )
    return initialization


@pytest.mark.parametrize("consumer", ["initial", "neutral", "native_conformance"])
def test_real_shared_capture_admission_waits_without_replaying_consumer(shared, consumer):
    accounting, policy, binding, capture_store = shared
    if consumer != "initial":
        ready(accounting, policy, binding)
    entered, done = Event(), Event()

    def admit():
        entered.set()
        if consumer == "initial":
            result = accounting.authorize_initialization(policy, binding)
        elif consumer == "neutral":
            result = accounting.authorize_neutral_start(policy, binding)
        else:
            result = accounting.require_native_conformance(policy, binding)
        done.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        with capture_store.transaction():
            future = pool.submit(admit)
            assert entered.wait(1)
            if done.wait(0.25) or future.done():
                future.result(timeout=1)
            assert not done.is_set()
        result = future.result(timeout=2)
    if consumer == "initial":
        assert result.native.binding.purpose == "host"
    elif consumer == "neutral":
        assert result.binding.purpose == "neutral"
    else:
        assert result == accounting.admissions.conformance.reference
    assert len(accounting.control.snapshot()["calls"]) == (3 if consumer == "neutral" else 2)


def test_real_accounting_known_host_stop_waits_before_snapshot(shared):
    accounting, policy, binding, _ = shared
    initialization = accounting.authorize_initialization(policy, binding)
    for permit in (initialization.native, initialization.relay):
        with permit.launch_boundary() as report:
            report(process_pid=None, process_start_identity=None)
    stop = GroupStop(True, GroupObservation(True, ()), (), ())
    entered, done = Event(), Event()

    def settle():
        entered.set()
        accounting.record_host_stop(binding, stop)
        done.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with accounting.control.effect_boundary(
            initialization.native.reference, worker_identity=binding.worker_id
        ):
            future = pool.submit(settle)
            assert entered.wait(1)
            if done.wait(0.25) or future.done():
                future.result(timeout=1)
            assert not done.is_set()
        future.result(timeout=2)
    assert all(c["process_stopped"] for c in accounting.control.snapshot()["calls"])


@pytest.mark.parametrize(
    "change", ["cancel", "capture_revoke", "permission_revoke", "capture_expiry"]
)
def test_shared_capture_wait_keeps_original_auth_and_capture_authority(shared, monkeypatch, change):
    accounting, policy, binding, capture_store = shared
    accounting.authorize_initialization(policy, binding)
    busy, cancel = busy_event(monkeypatch), Event()
    reference = accounting.admissions.conformance.reference
    if change == "capture_expiry":
        with capture_store.transaction() as conn:
            row = capture_store.read(conn, "native_conformance", reference)
            row["valid_until"] = time.time() + 0.15
            capture_store.put(conn, "native_conformance", reference, row)

    def authenticate():
        with accounting.authentication_boundary(policy, binding, cancelled=cancel):
            return accounting.authorize_auth(policy, binding)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with capture_store.transaction() as conn:
            future = pool.submit(authenticate)
            assert busy.wait(1)
            if change == "cancel":
                cancel.set()
            elif change == "capture_revoke":
                row = capture_store.read(conn, "native_conformance", reference)
                row["revoked"] = True
                capture_store.put(conn, "native_conformance", reference, row)
            elif change == "permission_revoke":
                accounting.control.store.revoke(accounting.control.reference)
            if change in {"cancel", "capture_expiry"}:
                with pytest.raises(AccessDenied):
                    future.result(timeout=1)
        if change in {"capture_revoke", "permission_revoke"}:
            with pytest.raises(AccessDenied):
                future.result(timeout=1)
    assert len(accounting.control.snapshot()["calls"]) == 2
    assert accounting._authentication_window is None


def test_observed_turn_completion_uses_its_existing_finite_permit(shared):
    accounting, policy, binding, _ = shared
    host = ready(accounting, policy, binding).native
    permit = accounting.authorize_neutral_start(policy, binding)
    with permit.launch_boundary() as report:
        report(process_pid=None, process_start_identity=None)
    entered, done = Event(), Event()

    def complete():
        entered.set()
        accounting.record_neutral_start(
            permit,
            outcome="observed_turn",
            result=None,
            observation_ref="host-record:authored-turn",
        )
        done.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with accounting.control.effect_boundary(host.reference, worker_identity=binding.worker_id):
            future = pool.submit(complete)
            assert entered.wait(1)
            if done.wait(0.25) or future.done():
                future.result(timeout=1)
            assert not done.is_set()
        future.result(timeout=2)
    calls = {row["call_id"]: row for row in accounting.control.snapshot()["calls"]}
    assert calls[permit.reference]["process_stopped"] is True
    assert calls[host.reference]["process_stopped"] is False


@pytest.mark.parametrize("confirmed", [False, None])
def test_unknown_or_none_host_stop_never_clears_owned_calls(shared, confirmed):
    accounting, policy, binding, _ = shared
    ready(accounting, policy, binding)
    stop = GroupStop(confirmed, GroupObservation(False))
    if confirmed is None:
        with pytest.raises(AccessDenied):
            accounting.record_host_stop(binding, stop)
    else:
        accounting.record_host_stop(binding, stop)
    assert all(not row["process_stopped"] for row in accounting.control.snapshot()["calls"])


def test_shared_capture_wait_still_rejects_detached_copied_writer(shared, monkeypatch, tmp_path):
    import shutil

    from ai_task_manager.installation.native import store as store_module

    accounting, _, _, capture_store = shared
    path = capture_store.path
    copy, saved, opened = [
        tmp_path / name for name in ("copy.sqlite", "saved.sqlite", "opened.sqlite")
    ]
    original_bytes = path.read_bytes()
    shutil.copyfile(path, copy)
    copy.chmod(0o600)
    connect = store_module.sqlite3.connect
    replaced = []

    def detached(database, *args, **kwargs):
        if database != path:
            return connect(database, *args, **kwargs)
        path.rename(saved)
        copy.rename(path)
        connection = connect(database, *args, **kwargs)
        path.rename(opened)
        saved.rename(path)
        replaced.append(True)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", detached)
    with pytest.raises(AccessDenied):
        accounting.check_admission("claude")
    assert replaced == [True]
    assert path.read_bytes() == original_bytes == opened.read_bytes()
