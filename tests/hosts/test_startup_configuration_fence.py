"""Late startup observations through the actual graph; no native account/model call."""

import json
import os
from copy import copy
from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.startup import HostStartupReceipts
from ai_task_manager.config.files import FileSeal
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_policy import policy as native_policy


def late_change(composition, known, phase):
    value = composition()
    runtime, task, _, workers, factory, provider, _ = value
    changed = []

    def mutate(native):
        assert native.startup.ready_ref is not None
        previous = native.startup.ready_ref
        source = native.policy.codex_settings.sources.home
        replacement = source / "controlled-refresh"
        replacement.write_text("authored-cache-replacement-no-credential")
        replacement.chmod(0o600)
        replacement.replace(source / "auth.json")
        if known:
            with pytest.raises(AccessDenied):
                native.startup.assert_stable()
        changed.append(
            {
                "ready_ref_unchanged": native.startup.ready_ref == previous,
                "configuration_fence_known": native.startup.configuration.fenced,
            }
        )

    if phase == "before_proof":
        launch = factory.launch

        def launch_with_hook(binding, pair):
            native = launch(binding, pair)
            initialize = native.initialize

            def initialized_then_changed():
                result = initialize()
                mutate(native)
                return result

            native.initialize = initialized_then_changed
            return native

        factory.launch = launch_with_hook
    else:
        startup = workers.proofs.startup

        def proof_then_changed(scope, attempt_id, observation_id, *, host_ready_ref=None):
            proof = startup(scope, attempt_id, observation_id, host_ready_ref=host_ready_ref)
            mutate(factory.owned[attempt_id])
            return proof

        workers.proofs.startup = proof_then_changed
    error_name = None
    try:
        start(value)
    except (AccessDenied, EvidenceRequired) as error:
        error_name = type(error).__name__
    with runtime.rig.db.transaction() as conn:
        started = conn.execute(
            "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='worker_started'", (task.id,)
        ).fetchone()[0]
    current = runtime.rig.core.get_task(runtime.rig.handle, task.id)
    return {
        "phase": phase,
        "known_requested": known,
        "mutation": changed,
        "core_worker_started_events": started,
        "provider_confirmation_calls": len(provider.calls),
        "observed_status": current.observed_status,
        "desired_status": current.desired_status,
        "error": error_name,
    }


def persist(name, observations):
    directory = os.environ.get("AITM_TEST_EVIDENCE_DIR")
    if directory:
        Path(directory, name + ".json").write_text(
            json.dumps(
                {
                    "scope": "Authored owned process/provider controls; no native acceptance",
                    "observations": observations,
                },
                indent=2,
            )
            + "\n"
        )


def test_known_configuration_fence_precedes_core_start_for_every_proof_phase(composition):
    observations = [
        late_change(composition, True, phase) for phase in ("before_proof", "before_core")
    ]
    persist("late-known-configuration-fence", observations)
    assert all(
        row["core_worker_started_events"] == 0 and row["provider_confirmation_calls"] == 0
        for row in observations
    ), observations


def test_unobserved_cache_change_is_rechecked_at_startup_proof_consumption(composition):
    observations = [
        late_change(composition, False, phase) for phase in ("before_proof", "before_core")
    ]
    persist("late-unobserved-cache-change", observations)
    assert all(
        row["core_worker_started_events"] == 0 and row["provider_confirmation_calls"] == 0
        for row in observations
    ), observations


def after_proof(value, mutation):
    workers, factory = value[3:5]
    original = workers.proofs.startup
    observed = []

    def hooked(scope, attempt_id, observation_id, *, host_ready_ref=None):
        proof = original(scope, attempt_id, observation_id, host_ready_ref=host_ready_ref)
        native = factory.owned[attempt_id]
        observed.append(native)
        mutation(native)
        return proof

    workers.proofs.startup = hooked
    return observed


def assert_no_start(value):
    runtime, task, _, _, _, provider, _ = value
    with pytest.raises((AccessDenied, EvidenceRequired)):
        start(value)
    with runtime.rig.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='worker_started'", (task.id,)
            ).fetchone()[0]
            == 0
        )
    current = runtime.rig.core.get_task(runtime.rig.handle, task.id)
    assert current.observed_status == current.desired_status == "Todo"
    assert provider.calls == []


def guard_core_checks(value, monkeypatch):
    core = value[0].rig.core
    active = [False]
    original = core.execution.start

    def guarded(*args, **kwargs):
        active[0] = True
        try:
            return original(*args, **kwargs)
        finally:
            active[0] = False

    monkeypatch.setattr(core.execution, "start", guarded)
    launch = value[4].launch

    def launch_guarded(binding, pair):
        native = launch(binding, pair)
        initialize = native.initialize

        def forbid_when_active(original, label):
            def checked(*args, **kwargs):
                assert not active[0], label
                return original(*args, **kwargs)

            return checked

        def initialized():
            ref = initialize()
            for target, name in (
                (native.process, "verify_identity"),
                (native.startup, "assert_stable"),
                (native.journal, "_history"),
                (native.journal, "_append"),
            ):
                monkeypatch.setattr(target, name, forbid_when_active(getattr(target, name), name))
            policy_type = type(native.policy)
            monkeypatch.setattr(
                policy_type, "check", forbid_when_active(policy_type.check, "full policy check")
            )
            sources = native.policy.codex_settings.sources
            file_read = sources._file

            def checked_file(*args, **kwargs):
                assert not (active[0] and kwargs.get("content")), "source content under Core"
                return file_read(*args, **kwargs)

            monkeypatch.setattr(sources, "_file", checked_file)
            return ref

        native.initialize = initialized
        return native

    monkeypatch.setattr(value[4], "launch", launch_guarded)


def test_consumption_checks_metadata_without_full_validation_inside_core(composition, monkeypatch):
    value = composition()
    guard_core_checks(value, monkeypatch)
    dispatch = start(value)
    assert dispatch.state == "running"
    assert value[5].calls == [value[1].id]


def test_missing_live_owner_refuses_cached_worker_start(composition):
    value = composition()
    factory = value[4]
    seen = after_proof(value, lambda native: factory.owned.pop(native.binding.attempt_id))
    try:
        assert_no_start(value)
    finally:
        for native in seen:
            factory.owned[native.binding.attempt_id] = native


def test_replaced_live_owner_refuses_cached_worker_start(composition):
    value = composition()
    factory = value[4]

    def replaced(native):
        factory.owned[native.binding.attempt_id] = copy(native)

    seen = after_proof(value, replaced)
    try:
        assert_no_start(value)
    finally:
        for native in seen:
            factory.owned[native.binding.attempt_id] = native


@pytest.mark.parametrize("component", ["observer", "registered_process"])
def test_replaced_observer_or_process_refuses_cached_worker_start(composition, component):
    value = composition()
    factory = value[4]
    originals = []

    def replaced(native):
        if component == "observer":
            originals.append(native.startup)
            native.startup = copy(native.startup)
        else:
            originals.append(native.process)
            factory.process_registry._owned[native.binding.attempt_id] = copy(native.process)

    seen = after_proof(value, replaced)
    try:
        assert_no_start(value)
    finally:
        for native, original in zip(seen, originals, strict=True):
            if component == "observer":
                native.startup = original
            else:
                factory.process_registry._owned[native.binding.attempt_id] = original


def test_original_startup_binding_and_expiry_are_not_renewed(composition, monkeypatch):
    value = composition()
    dispatch = start(value)
    factory = value[4]
    verifier = factory.startup_verifier
    native = factory.owned[dispatch.id]
    original = verifier._prepared[native.startup.ready_ref].observed
    assert verifier.check_prepared_host_ready(original) is original
    for changed in (
        replace(original, worker_id="foreign-worker"),
        replace(original, expires_at=original.expires_at + 60),
        replace(original, observation_ref="host-record:unprepared"),
    ):
        with pytest.raises(AccessDenied):
            verifier.check_prepared_host_ready(changed)
    monkeypatch.setattr(verifier.journal, "clock", lambda: original.expires_at + 1)
    with pytest.raises(AccessDenied):
        verifier.check_prepared_host_ready(original)


def test_source_metadata_change_refuses_before_core_start_without_content_read(
    composition, monkeypatch
):
    value = composition()
    guard_core_checks(value, monkeypatch)
    changed = []

    def mutate(native):
        path = native.policy.codex_settings.sources.home / "config.toml"
        previous = path.read_text()
        path.write_text(previous + "# authored changed source\n")
        changed.append((native, path, previous))

    after_proof(value, mutate)
    assert_no_start(value)
    native, path, previous = changed[0]
    path.write_text(previous)
    with pytest.raises(AccessDenied):
        native.startup.model_guard.check_current()


def test_sticky_model_refusal_after_proof_prevents_start(composition, monkeypatch):
    value = composition()
    guard_core_checks(value, monkeypatch)

    def refuse(native):
        # Authored negative control; it is not a native observation or receipt.
        native.startup.model_guard.refusal = "authored-existing-sticky-refusal"

    after_proof(value, refuse)
    assert_no_start(value)


def test_process_only_startup_verifier_cannot_consume_prepared_authority(composition):
    value = composition()
    dispatch = start(value)
    factory = value[4]
    native = factory.owned[dispatch.id]
    process_only = HostStartupReceipts(factory.journal, factory.process_registry)
    original = process_only.verify_host_ready(native.startup.ready_ref)
    with pytest.raises(AccessDenied):
        process_only.check_prepared_host_ready(original)


@pytest.mark.parametrize("component", ["guard", "configuration", "guard_configuration"])
def test_replaced_guard_or_configuration_refuses_cached_worker_start(composition, component):
    value = composition()

    def replaced(native):
        observer = native.startup
        if component == "guard":
            observer.model_guard = copy(observer.model_guard)
        elif component == "configuration":
            observer.configuration = copy(observer.configuration)
        else:
            observer.model_guard.configuration = copy(observer.configuration)

    after_proof(value, replaced)
    assert_no_start(value)


def test_prepared_policy_seals_check_metadata_without_content_reads(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Prepared seal consumption must not read source contents")

    for change in ("edit", "replace", "mode", "missing"):
        root = tmp_path / change
        root.mkdir()
        source = root / "private-source.json"
        source.write_text("{}")
        source.chmod(0o600)
        policy = replace(native_policy(root, "claude"), seals=(FileSeal(source),))
        prepared = policy.prepare_admission()
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "read_bytes", forbidden)
            policy.check_admission_metadata(prepared)
            if change == "edit":
                source.write_text('{"authored":"changed"}')
            elif change == "replace":
                new = root / "new.json"
                new.write_text("{}")
                new.chmod(0o600)
                new.replace(source)
            elif change == "mode":
                source.chmod(0o644)
            else:
                source.unlink()
            with pytest.raises(AccessDenied):
                policy.check_admission_metadata(prepared)
