"""Actual owned synthetic native processes; these are not account/model evidence."""

from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start
from tests.hosts.test_managed_terminal import running_managed as running_managed
from tests.hosts.test_native_boundaries import bridge_fixture


@pytest.mark.parametrize("kind", ["current", "expired", "replaced_policy", "exited"])
def test_owned_native_continuity_preserves_original_session_and_expiry_fence(composition, kind):
    graph = composition()
    attempt = start(graph)
    native = graph[4].owned[attempt.id]
    current = NativeContinuity(native)
    original = native.startup.ready_ref
    original_policy = native.policy
    if kind == "expired":
        current.expires_at = native.journal.clock() - 1
    elif kind == "replaced_policy":
        native.policy = SimpleNamespace()
    elif kind == "exited":
        native.stop()
    if kind != "current":
        try:
            with pytest.raises(AccessDenied):
                current.prepare()
        finally:
            native.policy = original_policy
        return
    prepared, reference = current.prepare()
    current.acknowledge(prepared)
    assert native.startup.ready_ref == original
    record = native.journal._read(reference)
    assert record["payload"]["new_authentication"] is False
    assert record["payload"]["execution_deadline"] == native.binding.deadline
    assert record["expires_at"] <= native.binding.deadline
    current.close()
    with pytest.raises(AccessDenied):
        current.check(prepared)


def test_async_disconnect_is_awaited_once_even_after_repeated_owned_stop(
    running_managed, journal, tmp_path, recwarn
):
    _, _, _, _, bridge, _ = bridge_fixture(running_managed, journal, tmp_path)
    calls = []

    async def close():
        calls.append("awaited")

    bridge.session = SimpleNamespace(close=close)
    assert bridge.stop().confirmed
    assert bridge.stop().confirmed
    assert calls == ["awaited"]
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


class AuthoredDependencyCollection:
    """Explicit process fixture input; never a production provider observation."""

    def __init__(self, core, handle):
        self.core, self.handle = core, handle
        self.binding = core.session_binding(handle)
        self.calls = 0
        self.fail = False
        self.failed = Event()

    def prepare(self, task_id, operation_key, *, expected_version, decision_revision):
        from tests.scheduling.helpers import snapshot

        task = self.core.get_task(self.handle, task_id)
        self.core.context.check(task, expected_version, decision_revision)
        if self.fail:
            self.failed.set()
            raise AccessDenied("Authored dependency collection failure")
        self.calls += 1
        current = snapshot(self.core, self.handle, task, self.core.context.clock())
        from ai_task_manager.adapters.linear.dependencies.source import Preparation

        return Preparation(
            task.id,
            task.version,
            task.decision_revision,
            self.core.scheduling.sources.issue(current),
            operation_key,
        )

    def refresh_for_start(self, task_id, operation_key, *, expected_version, decision_revision):
        value = self.prepare(
            task_id,
            operation_key,
            expected_version=expected_version,
            decision_revision=decision_revision,
        )
        return self.core.scheduling.observe(
            self.handle,
            task_id,
            expected_version,
            decision_revision,
            value.snapshot_ref,
            "authored-start-dependencies:" + str(uuid4()),
        )


@pytest.mark.parametrize("age_before_work", [False, True])
def test_real_managed_composition_refreshes_before_start_and_lease_gated_edit(
    composition, journal, age_before_work
):
    value = composition()
    runtime, task, _, workers, factory, _, _ = value
    core, handle = runtime.rig.core, runtime.rig.handle
    clock = [journal.clock()]
    wall = journal.clock
    core.context.clock = journal.clock = lambda: max(clock[0], wall())
    workers.deadline = clock[0] + 55
    source = AuthoredDependencyCollection(core, handle)
    workers.bind_dependencies(source)
    attempt = start(value)
    authority = workers._freshness[attempt.id]
    refreshed = [
        e
        for e in core.notifications.list_events(handle)
        if e.kind == "scheduling_authority_renewed"
    ]
    assert refreshed
    cap = factory.owned[attempt.id].binding.deadline
    assert all(e.payload["expires_at"] <= cap for e in refreshed)
    current_facts = core.scheduling.explain(handle, task.id)
    assert current_facts["state"] == "ready"
    assert current_facts["source_revision"] >= refreshed[-1].payload["source_revision"]
    if age_before_work:
        clock[0] += 21
    workers.execute_reviewed(handle, attempt.id, "assignment", "fresh-assignment")
    assert (
        workers.dispatches[attempt.id].resources.worktree / "owned.py"
    ).read_text() == "answer = 42\n"
    assert core.get_attempt(handle, attempt.id).state == "done"
    assert factory.owned[attempt.id].process._closed
    assert authority.close()
    assert factory.owned[attempt.id].binding.deadline == cap
    assert source.calls >= (3 if age_before_work else 2)


def test_background_freshness_loss_records_unknown_and_retains_resource_ownership(
    composition, journal
):
    value = composition()
    runtime, _, _, workers, factory, _, _ = value
    core, handle = runtime.rig.core, runtime.rig.handle
    clock = [journal.clock()]
    wall = journal.clock
    core.context.clock = journal.clock = lambda: max(clock[0], wall())
    workers.deadline = clock[0] + 55
    source = AuthoredDependencyCollection(core, handle)
    workers.bind_dependencies(source)
    attempt = start(value)
    authority = workers._freshness[attempt.id]
    source.fail = True
    clock[0] += 21
    assert source.failed.wait(8)
    authority._thread.join(timeout=3)
    assert not authority._thread.is_alive()
    assert factory.owned[attempt.id].process._closed
    assert core.get_attempt(handle, attempt.id).state == "unknown"
    with core.context.db.transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)
            ).fetchone()[0]
            == 2
        )
    assert not (workers.dispatches[attempt.id].resources.worktree / "owned.py").exists()
    stopped = workers.stop(handle, attempt.id, operation_key="recover-freshness-loss")
    assert stopped.state == "paused"


@pytest.mark.parametrize("contended", [False, True])
def test_task_scoped_managed_lifecycle_renews_and_settles_retained_resources(
    composition, request, monkeypatch, contended
):
    """Complete local process/Core lifecycle with authored native/provider ports."""
    import secrets
    from dataclasses import asdict

    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
    from ai_task_manager.application.workflows.recovery import SuccessorAuthorization
    from ai_task_manager.domains.work.models import Evidence, SessionHandle
    from ai_task_manager.storage.work import fingerprint

    value = composition()
    runtime, task, _, workers, factory, _, _ = value
    r = runtime.rig
    workers.deadline = runtime.journal.clock() + 55
    prior = start(value)
    original = workers.dispatches[prior.id].resources
    (original.worktree / "retained.txt").write_text("retained local fixture work\n")
    assert workers.stop(r.handle, prior.id, operation_key="settle-predecessor").state == "paused"
    task = r.core.get_task(r.handle, task.id)
    r.core.context.evidence = EvidenceRouter([
        EvidenceRoute("runtime-proof:", RUNTIME_KINDS, workers.proofs),
        EvidenceRoute("fixture:", frozenset({
            "provider_status", "decision", "human_decision", "workflow_successor"
        }), r.proofs),
    ])
    expires = r.core.context.clock() + 120
    decision = r.proof("decision", task.id, {
        "target": "In Progress", "revision": task.decision_revision, "actor": "agent"
    }, expires_at=expires)
    authorization = SuccessorAuthorization(
        r.key(), r.scope, "workflow:predecessor", task.id, task.version,
        task.decision_revision, "fresh-plan", "fresh-digest", "fresh-key",
        expires, decision, tuple(task.required_work),
    )
    r.proofs.evidence[authorization.reference] = Evidence(
        "workflow_successor", r.scope, task.id, asdict(authorization), expires
    )
    handle = SessionHandle(secrets.token_urlsafe(32))
    r.core.revoke_session(r.handle)
    r.core.admit_workflow_successor(
        r.handle, handle, authorization, lambda *_: "workflow:successor-local"
    )
    runtime.resources.successor_sources[task.id] = prior.id
    from dataclasses import replace

    from ai_task_manager.adapters.hosts.bridge.codex_home import CodexHomeBoundary
    from ai_task_manager.adapters.hosts.bridge.codex_settings import CodexSettings
    from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
    from ai_task_manager.adapters.hosts.bridge.policy import policy_digest
    from ai_task_manager.adapters.proofs.store import RuntimeProofs
    from ai_task_manager.supervisor.managed import ManagedWorkers

    original_factory = factory
    original_policy = factory.policies("codex", original)
    settings = CodexSettings(
        CodexHomeBoundary(original_policy.codex_settings.sources.home),
        original_policy.model, original_policy.effort,
    )

    def successor_policy(host, pair):
        policy = original_factory.policies(host, pair)
        return replace(policy, codex_settings=settings, configuration_digest=policy_digest(
            host, policy.executable_sha256, dict(policy.environment), pair.worktree,
            {"codex_settings": settings.fingerprint}, model=policy.model, effort=policy.effort,
        ))

    factory = ManagedHostFactory(
        r.core, runtime.resources, original_factory.journal,
        original_factory.root / "successor", successor_policy,
        original_factory.permits, original_factory.accounts,
    )

    def close_native():
        for native in factory.owned.values():
            if native.process is not None and not native.process._closed:
                native.stop()

    request.addfinalizer(close_native)
    proofs = RuntimeProofs(
        runtime.journal, workers.proofs._key_path,
        host_startups=factory.startup_verifier, existing_only=True,
    )
    r.core.context.evidence = EvidenceRouter([
        EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
        EvidenceRoute("fixture:", frozenset({
            "provider_status", "decision", "human_decision", "workflow_successor"
        }), r.proofs),
    ])
    workers = ManagedWorkers(
        r.core, runtime.journal, runtime.resources, runtime.runtime, proofs,
        workers.provider, factory, deadline=workers.deadline,
        principal_ref="fixture-successor-principal", executions=workers.executions,
        dependencies=AuthoredDependencyCollection(r.core, handle),
    )
    factory.commands = workers
    attempt = workers.start(
        handle, task.id, "codex", expected_version=task.version,
        decision_revision=task.decision_revision, decision_ref=decision,
        operation_key="workflow:successor-local:start",
    )
    assert attempt.state == "running"
    freshness = workers._freshness[attempt.id]
    if contended:
        from threading import Thread, current_thread

        guard = factory.owned[attempt.id].startup.model_guard
        prepared = guard.prepare_effect()
        held, release, preparing, proceed, completed = (Event() for _ in range(5))
        failures = []
        actual_prepare = guard.prepare_effect

        def observed_prepare(*args, **kwargs):
            if current_thread().name == "controlled-renewal":
                preparing.set()
                assert proceed.wait(5)
            return actual_prepare(*args, **kwargs)

        monkeypatch.setattr(guard, "prepare_effect", observed_prepare)

        def hold_effect():
            with guard.current_boundary(prepared):
                held.set()
                assert release.wait(5)

        def renew_contended():
            try:
                freshness.refresh()
            except BaseException as error:
                failures.append(error)
            finally:
                completed.set()

        holder = Thread(target=hold_effect)
        renewal = Thread(target=renew_contended, name="controlled-renewal")
        renewal.start()
        try:
            assert preparing.wait(3)
            holder.start()
            assert held.wait(3)
            proceed.set()
            assert not completed.is_set()
            with r.db.transaction() as conn:
                assert conn.execute("SELECT COUNT(*) FROM task_capabilities").fetchone()[0] == 1
        finally:
            proceed.set()
            release.set()
            if holder.ident is not None:
                holder.join(5)
            renewal.join(5)
        assert not holder.is_alive() and not renewal.is_alive()
        assert completed.is_set() and not failures
    else:
        freshness.refresh()
    events = [e for e in r.core.notifications.list_events(handle)
              if e.kind == "scheduling_authority_renewed"]
    assert events
    reference = events[-1].payload["observation_ref"]
    with r.db.transaction() as conn:
        key, recorded = conn.execute(
            "SELECT key,fingerprint FROM operations WHERE key LIKE 'work-freshness:%:commit' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert recorded == fingerprint({
        "command": "renew_work_authority", "observation_ref": reference,
        "task_id": task.id, "attempt_id": attempt.id,
    })
    before = len(events)
    assert r.core.scheduling.renew(handle, reference, key)["state"] == "ready"
    assert len([e for e in r.core.notifications.list_events(handle)
                if e.kind == "scheduling_authority_renewed"]) == before
    pair = workers.dispatches[attempt.id].resources
    assert pair.worktree == original.worktree and pair.database == original.database
    workers.execute_reviewed(handle, attempt.id, "assignment", "successor-owned-assignment")
    assert (pair.worktree / "owned.py").read_text() == "answer = 42\n"
    assert (pair.worktree / "retained.txt").read_text() == "retained local fixture work\n"
    assert r.core.get_attempt(handle, attempt.id).state == "done"
    assert r.core.get_attempt(handle, prior.id).state == "paused"
    assert factory.owned[attempt.id].process._closed and freshness.close()
    r.core.revoke_session(handle)
    with pytest.raises(AccessDenied):
        r.core.scheduling.renew(handle, reference, key)
    with r.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 0
