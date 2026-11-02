"""Bounded descriptor recovery and owned lifecycle controls; no native accounts."""

import json
import os
import sys
from dataclasses import replace

import pytest

from ai_task_manager.adapters.hosts.bridge import codex_auth, codex_home, darwin_acl
from ai_task_manager.adapters.hosts.bridge.codex_home import AclAuditPort, CodexHomeBoundary
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.acl_fixtures import set_fixture_acl
from tests.hosts.codex_configuration_helpers import files
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_startup_configuration_fence import (
    after_proof,
    assert_no_start,
    guard_core_checks,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin descriptor controls")


def source(tmp_path, journal):
    guard = CodexHomeBoundary(files(tmp_path))
    context = json.dumps(
        {
            "host": "codex",
            "attempt_id": "fixture-attempt",
            "scope": {
                "provider": "fixture",
                "workspace": "fixture",
                "principal": "fixture",
                "project": "fixture",
                "logical_session": "fixture",
            },
            "worktree_id": "fixture-tree",
            "database_id": "fixture-db",
        },
        sort_keys=True,
    )
    port = AclAuditPort(guard, journal, context)
    guard.bind_acl_auditor(port)
    port()
    return guard, port


def churn(monkeypatch, target, count):
    original = darwin_acl.DarwinAclApi.get
    identity = (target.stat().st_dev, target.stat().st_ino)
    state = {"calls": 0, "changed": 0}

    def get(api, fd):
        result = original(api, fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            state["calls"] += 1
            if state["changed"] < count:
                leaf = ".owned-observation-churn"
                child = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                os.close(child)
                os.unlink(leaf, dir_fd=fd)
                state["changed"] += 1
        return result

    monkeypatch.setattr(darwin_acl.DarwinAclApi, "get", get)
    return state


def inspect_home(guard):
    fd = guard._open(guard.home, directory=True, owned_directory=True)
    os.close(fd)


def trials_for(guard):
    import hashlib

    wanted = hashlib.sha256(str(guard.home).encode()).hexdigest()
    return [
        row
        for raw in guard.pending_acl_observations()
        if (row := json.loads(raw))["path_hash"] == wanted
    ]


@pytest.mark.parametrize("variant", ["allowed"])
def test_transient_ctime_gets_one_complete_fresh_observation(
    tmp_path, journal, monkeypatch, variant
):
    guard, port = source(tmp_path, journal)
    state = churn(monkeypatch, guard.home, 1)
    inspect_home(guard)
    assert state == {"calls": 2, "changed": 1}
    rows = trials_for(guard)
    assert len(rows) == 1 and len(rows[0]["trials"]) == 2
    assert rows[0]["trials"][0]["acl"]["kind"] == "unknown"
    assert rows[0]["trials"][1]["decision"] == "accepted"
    before = guard.pending_acl_observations()
    assert isinstance(before, tuple) and all(isinstance(row, str) for row in before)
    port()
    assert not guard.pending_acl_observations()
    assert any(
        r["payload"].get("effect") == "acl_descriptor_observations"
        for r in journal._history("trace")
    )


@pytest.mark.parametrize("variant", ["stable", "changed"])
def test_observed_grant_is_never_recovered(tmp_path, journal, monkeypatch, variant):
    guard, _ = source(tmp_path, journal)
    set_fixture_acl(guard.home, (1,))
    state = churn(monkeypatch, guard.home, 1 if variant == "changed" else 0)
    with pytest.raises(AccessDenied):
        inspect_home(guard)
    assert state["calls"] == 1
    rows = trials_for(guard)
    assert rows and rows[-1]["trials"][0]["acl"]["acl_kind"] == "has_allow"


@pytest.mark.parametrize("field", ["mode", "uid", "gid", "inode", "links", "path"])
def test_security_or_path_change_never_reuses_posix(tmp_path, journal, monkeypatch, field):
    guard, _ = source(tmp_path, journal)
    identity = (guard.home.stat().st_dev, guard.home.stat().st_ino)
    if field == "path":
        original = darwin_acl.DarwinAclApi.get
        changed = []

        def get(api, fd):
            result = original(api, fd)
            if not changed and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == identity:
                guard.home.rename(guard.home.with_name("retained-original"))
                guard.home.mkdir(mode=0o700)
                changed.append(True)
            return result

        monkeypatch.setattr(darwin_acl.DarwinAclApi, "get", get)
        with pytest.raises(AccessDenied):
            guard.check_metadata()
        assert changed
        return
    original = darwin_acl._metadata
    count = 0

    def metadata(fd):
        nonlocal count
        value = original(fd)
        if value[:2] == identity:
            count += 1
            if count == 2:
                result = list(value)
                result[{"mode": 4, "uid": 2, "gid": 3, "inode": 1, "links": 5}[field]] += 1
                return tuple(result)
        return value

    monkeypatch.setattr(darwin_acl, "_metadata", metadata)
    with pytest.raises(AccessDenied):
        inspect_home(guard)
    assert count == 2


@pytest.mark.parametrize("variant", ["churn", "api", "release"])
def test_repeated_churn_or_unavailable_inspection_refuses(tmp_path, journal, monkeypatch, variant):
    guard, _ = source(tmp_path, journal)
    if variant == "churn":
        state = churn(monkeypatch, guard.home, 2)
    else:
        original = darwin_acl.inspect_acl
        identity = guard.home.stat().st_ino
        state = {"calls": 0}

        def inspect(fd):
            result = original(fd)
            if os.fstat(fd).st_ino == identity:
                state["calls"] += 1
                return replace(
                    result,
                    kind=darwin_acl.AclKind.UNKNOWN,
                    reason="acl_api_unavailable" if variant == "api" else "acl_copy_release_failed",
                )
            return result

        monkeypatch.setattr(darwin_acl, "inspect_acl", inspect)
    with pytest.raises(AccessDenied):
        inspect_home(guard)
    assert state["calls"] == (2 if variant == "churn" else 1)
    assert trials_for(guard)


@pytest.mark.parametrize("variant", ["config", "credential"])
def test_reobservation_does_not_reseal_configuration(tmp_path, journal, monkeypatch, variant):
    guard, _ = source(tmp_path, journal)
    original = guard.credential_metadata()
    (guard.home / ("config.toml" if variant == "config" else "auth.json")).write_text(
        "changed fixture"
    )
    churn(monkeypatch, guard.home, 1)
    with pytest.raises(AccessDenied):
        guard.check() if variant == "config" else guard.check_readonly_credential(original)


@pytest.mark.parametrize("variant", ["recover", "repeat"])
def test_core_rechecks_without_logging_or_content_reads(composition, monkeypatch, variant):
    value = composition()
    guard_core_checks(value, monkeypatch)

    def inject(native):
        original = darwin_acl._metadata
        identity = native.policy.codex_settings.sources.home.stat().st_ino
        count = 0

        def metadata(fd):
            nonlocal count
            values = original(fd)
            if values[1] == identity:
                count += 1
                if count == 2 or variant == "repeat" and count == 4:
                    values = (*values[:6], values[6] + 1)
            return values

        monkeypatch.setattr(darwin_acl, "_metadata", metadata)

    seen = after_proof(value, inject)
    assert_no_start(value)  # Pending recovered diagnostics also require off-Core acknowledgement.
    assert len(seen) == 1


@pytest.mark.parametrize("variant", ["ack", "write_failure", "overflow"])
def test_audit_retention_ack_and_overflow_are_fail_closed(tmp_path, journal, monkeypatch, variant):
    guard, port = source(tmp_path, journal)
    state = churn(monkeypatch, guard.home, 1)
    inspect_home(guard)
    pending = guard.pending_acl_observations()
    assert pending
    with pytest.raises(AccessDenied):
        guard.acknowledge_acl_observations(pending)
    if variant == "ack":
        port()
        assert not guard.pending_acl_observations()
    elif variant == "write_failure":

        def fail(*args, **kwargs):
            raise OSError("authored audit failure")

        monkeypatch.setattr(journal, "_append", fail)
        with pytest.raises(AccessDenied):
            port()
        assert guard.pending_acl_observations() == pending
    else:
        monkeypatch.setattr(guard, "ACL_AUDIT_LIMIT", len(pending))
        with pytest.raises(AccessDenied):
            inspect_home(guard)
        assert guard.pending_acl_observations() == pending
        assert state["calls"] == 2


@pytest.mark.parametrize("variant", ["generation", "method", "custom"])
def test_new_generation_and_default_method_drift_refuse(tmp_path, journal, monkeypatch, variant):
    guard, _ = source(tmp_path, journal)
    if variant == "custom":
        with pytest.raises(AccessDenied):
            CodexHomeBoundary(files(tmp_path / "custom"), acl_query=lambda fd: None)
        return
    if variant == "generation":
        monkeypatch.setattr(codex_home, "ACL_POLICY_GENERATION", "authored-other-generation")
    else:
        monkeypatch.setattr(type(codex_home.no_acl_grants), "inspect", lambda self, fd: None)
    with pytest.raises(AccessDenied):
        guard.check_metadata()


@pytest.mark.parametrize("variant", ["recover", "repeat"])
def test_actual_status_boundary_retains_deadlines_and_churn(composition, monkeypatch, variant):
    value = composition()
    active = [False]
    supervise = codex_auth.supervise
    builder = value[4].policies
    policies = []

    def policy(host, pair):
        result = builder(host, pair)
        policies.append(result)
        return result

    monkeypatch.setattr(value[4], "policies", policy)
    original = darwin_acl.DarwinAclApi.get
    state = {"changed": 0}

    def get(api, fd):
        result = original(api, fd)
        if (
            active[0]
            and policies
            and os.fstat(fd).st_ino == policies[-1].codex_settings.sources.home.stat().st_ino
            and state["changed"] < (2 if variant == "repeat" else 1)
        ):
            child = os.open(
                ".owned-status-churn", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd
            )
            os.close(child)
            os.unlink(".owned-status-churn", dir_fd=fd)
            state["changed"] += 1
        return result

    monkeypatch.setattr(darwin_acl.DarwinAclApi, "get", get)
    outcomes = []

    def monitored(group, *, timeout_seconds, boundary_check=None):
        assert 0 < timeout_seconds <= 10
        active[0] = True
        try:
            result = supervise(
                group, timeout_seconds=timeout_seconds, boundary_check=boundary_check
            )
            outcomes.append(result)
            return result
        finally:
            active[0] = False

    monkeypatch.setattr(codex_auth, "supervise", monitored)
    if variant == "repeat":
        with pytest.raises(AccessDenied):
            start(value)
        assert outcomes[0]["outcome"] == "interrupted"
        assert outcomes[0]["reason"] == "authority_boundary:AccessDenied"
        assert not value[5].calls
    else:
        assert start(value).state == "running"
        assert outcomes[0]["outcome"] == "succeeded"
    assert state["changed"] == (2 if variant == "repeat" else 1)
    assert outcomes[0]["group"]["confirmed_stop"]


@pytest.mark.parametrize("variant", ["flush", "stop"])
def test_controller_flush_preserves_pending_on_journal_error(composition, monkeypatch, variant):
    value = composition()
    attempt = start(value)
    native = value[4].owned[attempt.id]
    guard = native.policy.codex_settings.sources
    churn(monkeypatch, guard.home, 1)
    inspect_home(guard)
    pending = guard.pending_acl_observations()
    append = native.journal._append

    def failing(kind, payload, **kwargs):
        if payload.get("effect") == "acl_descriptor_observations":
            raise OSError("authored flush failure")
        return append(kind, payload, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(native.journal, "_append", failing)
        with pytest.raises(AccessDenied) as error:
            native.stop() if variant == "stop" else native.verify_active()
        assert guard.pending_acl_observations() == pending
        if variant == "stop":
            assert native.process._closed and error.value.owned_stop_evidence.confirmed
    native._flush_source_observations()


@pytest.mark.parametrize("variant", ["cleared", "swapped"])
def test_first_audit_binding_survives_current_pointer_drift(
    tmp_path, journal, monkeypatch, variant
):
    guard, original = source(tmp_path, journal)
    churn(monkeypatch, guard.home, 1)
    inspect_home(guard)
    pending = guard.pending_acl_observations()
    assert pending
    replacement = AclAuditPort(guard, journal, original.context)
    history = journal._history("trace")
    with monkeypatch.context() as scoped:
        current = None if variant == "cleared" else replacement
        scoped.setattr(guard, "_acl_auditor", current)
        for requested in (replacement, original):
            with pytest.raises(AccessDenied):
                guard.bind_acl_auditor(requested)
            assert guard._acl_auditor is current
            assert guard._bound_acl_auditor is original
        for port in (original, replacement):
            with pytest.raises(AccessDenied):
                port()
            with pytest.raises(AccessDenied):
                guard.acknowledge_acl_observations(pending, auditor=port)
            assert guard.pending_acl_observations() == pending
            assert journal._history("trace") == history
    guard.bind_acl_auditor(original)
    assert guard._acl_auditor is guard._bound_acl_auditor is original
    original()
    assert not guard.pending_acl_observations()


@pytest.mark.parametrize("variant", ["different", "refusal"])
def test_factory_audits_each_actual_builder_source(composition, monkeypatch, variant):
    from ai_task_manager.adapters.hosts.bridge.codex_settings import CodexSettings

    value = composition()
    factory = value[4]
    original = factory.policies
    built = []

    def builder(host, pair):
        policy = original(host, pair)
        factory._audit_policy(policy, host, pair)
        fresh = CodexHomeBoundary(policy.codex_settings.sources.home)
        settings = CodexSettings(fresh, policy.model, policy.effort)
        with monkeypatch.context() as scoped:
            churn(scoped, fresh.home, 1)
            inspect_home(fresh)
        built.append(fresh)
        return replace(policy, codex_settings=settings)

    monkeypatch.setattr(factory, "policies", builder)
    if variant == "refusal":
        append = factory.journal._append

        def failure(kind, payload, **kwargs):
            if payload.get("effect") == "acl_descriptor_observations":
                raise OSError("authored unavailable audit")
            return append(kind, payload, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(factory.journal, "_append", failure)
            with pytest.raises(AccessDenied):
                start(value)
        assert built and built[0].pending_acl_observations()
        assert not factory.owned and not value[5].calls
    else:
        assert start(value).state == "running"
        assert len(built) == 2 and built[0] is not built[1]
        recorded = {
            r["payload"]["source_instance"]
            for r in factory.journal._history("trace")
            if r["payload"].get("effect") == "acl_descriptor_observations"
        }
        assert {s._acl_instance_id for s in built} <= recorded
        assert all(not s.pending_acl_observations() for s in built)


@pytest.mark.parametrize("variant", ["flush", "failure"])
def test_auth_ack_precedes_authorization_and_popen(
    tmp_path, journal, binding, monkeypatch, variant
):
    from tests.hosts.codex_configuration_helpers import FixturePermits, policy

    actual = policy(tmp_path)
    guard = actual.codex_settings.sources
    status_argv = (str(actual.executable), *actual.codex_settings.arguments(), "login", "status")
    context = json.dumps(
        {
            "host": "codex",
            "attempt_id": "fixture-attempt",
            "scope": {
                "provider": "fixture",
                "workspace": "fixture",
                "principal": "fixture",
                "project": "fixture",
                "logical_session": "fixture",
            },
            "worktree_id": "fixture-tree",
            "database_id": "fixture-db",
        },
        sort_keys=True,
    )
    port = AclAuditPort(guard, journal, context)
    guard.bind_acl_auditor(port)
    port()
    churn(monkeypatch, guard.home, 1)
    inspect_home(guard)
    assert guard.pending_acl_observations()
    owned = replace(binding, host="codex", configuration_digest=actual.configuration_digest)
    permits = FixturePermits()
    probe = codex_auth.CodexAuthProbe(journal, permits)
    popen = codex_auth.subprocess.Popen
    calls = []

    def launched(*args, **kwargs):
        if args and tuple(args[0]) == status_argv:
            assert not guard.pending_acl_observations()
            assert permits.calls[0] == "status-authorized"
            calls.append(tuple(args[0]))
        return popen(*args, **kwargs)

    monkeypatch.setattr(codex_auth.subprocess, "Popen", launched)
    if variant == "failure":
        flushes = []

        def fail(kind, payload, **kwargs):
            assert kind == "trace" and payload.get("effect") == "acl_descriptor_observations"
            flushes.append(payload["source_instance"])
            raise OSError("authored missing audit")

        monkeypatch.setattr(journal, "_append", fail)
        with pytest.raises(AccessDenied):
            probe.observe(actual, owned, tmp_path / "status")
        assert flushes == [guard._acl_instance_id]
        assert not calls and not permits.calls
        assert guard.pending_acl_observations()
    else:
        probe.observe(actual, owned, tmp_path / "status")
        assert calls == [status_argv] and permits.calls[0] == "status-authorized"


@pytest.mark.parametrize("variant", ["existing", "new"])
def test_metadata_consumption_never_flushes_pending(tmp_path, journal, monkeypatch, variant):
    guard, port = source(tmp_path, journal)
    churn(monkeypatch, guard.home, 1)
    if variant == "existing":
        inspect_home(guard)

    def forbidden(*args, **kwargs):
        raise AssertionError("Journal write in metadata-only consumption")

    monkeypatch.setattr(journal, "_append", forbidden)
    with pytest.raises(AccessDenied):
        guard.check_metadata()
    assert guard.pending_acl_observations()
    assert port is guard._acl_auditor


@pytest.mark.parametrize("variant", ["scope", "source", "journal", "rebind"])
def test_audit_ports_refuse_foreign_source_scope_or_journal(
    tmp_path, journal, monkeypatch, variant
):
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal

    guard, port = source(tmp_path, journal)
    if variant == "scope":
        with pytest.raises(AccessDenied):
            port.verify(guard, journal, port.context + " ")
    elif variant == "source":
        other = CodexHomeBoundary(files(tmp_path / "other"))
        with pytest.raises(AccessDenied):
            port.verify(other, journal, port.context)
    elif variant == "journal":
        other = HostJournal(tmp_path / "other-journal/state.sqlite", tmp_path / "other-journal/key")
        with pytest.raises(AccessDenied):
            port.verify(guard, other, port.context)
    else:
        foreign = AclAuditPort(guard, journal, port.context)
        with pytest.raises(AccessDenied):
            guard.bind_acl_auditor(foreign)
        monkeypatch.setattr(guard, "_acl_auditor", foreign)
        with pytest.raises(AccessDenied):
            port()


@pytest.mark.parametrize("variant", ["lease", "model"])
def test_lifecycle_preserves_original_refusal_after_audit_error(composition, monkeypatch, variant):
    from ai_task_manager.adapters.hosts.bridge.policy import NativePolicy

    value = composition()
    dispatch = start(value)
    native = value[4].owned[dispatch.id]
    guard = native.policy.codex_settings.sources
    churn(monkeypatch, guard.home, 1)
    original_error = AccessDenied("authored original refusal")

    def refusal():
        inspect_home(guard)
        raise original_error

    append = native.journal._append

    def fail(kind, payload, **kwargs):
        if payload.get("effect") == "acl_descriptor_observations":
            raise OSError("authored failed diagnostic flush")
        return append(kind, payload, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(native.journal, "_append", fail)
        if variant == "lease":
            check = NativePolicy.check

            def refused(policy):
                return refusal() if policy is native.policy else check(policy)

            scoped.setattr(NativePolicy, "check", refused)
            attempt = value[0].rig.core.get_attempt(value[0].rig.handle, dispatch.id)

            def action():
                return native._current_lease(value[0].rig.handle, attempt)
        else:
            scoped.setattr(native.gateway.delegate, "check", refusal)

            def action():
                return native.execute_model_turn("must never be submitted")

        with pytest.raises(AccessDenied) as raised:
            action()
        assert raised.value is original_error
        assert raised.value.acl_audit_error == "AccessDenied"
        assert guard.pending_acl_observations()
    native._flush_source_observations()
