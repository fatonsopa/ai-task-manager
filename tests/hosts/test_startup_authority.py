"""Real Core/provider-reader boundaries with authored provider data, never live auth."""

from dataclasses import asdict, replace
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from ai_task_manager.domains.scheduling.models import SchedulingBlocked
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import IssueStatus
from ai_task_manager.supervisor.authority import WorkAuthorities
from ai_task_manager.supervisor.journal import RuntimeJournal
from tests.scheduling.provider_fixture import ProviderFixture


def owned(tmp_path):
    provider = ProviderFixture(tmp_path / "provider")
    rig = provider.rig
    task = provider.task()
    task = rig.transition(task, IssueStatus.TODO, "enqueue")
    provider.sync(task)
    provider.refresh(task)
    task, attempt = rig.reserved(rig.current(task))
    runtime = RuntimeJournal(tmp_path / "runtime.sqlite", clock=lambda: rig.now)
    runtime.claim(attempt, asdict(rig.scope), deadline=rig.now + 120)
    registry = WorkAuthorities(rig.core, provider.source)
    rig.core.scheduling.renewals = registry
    startup = registry.starting(rig.handle, attempt, runtime, rig.now + 120)
    return provider, startup, attempt


def no_started_worker(provider, attempt):
    rig = provider.rig
    assert rig.core.get_attempt(rig.handle, attempt.id).worker_id is None
    assert not [e for e in rig.core.notifications.list_events(rig.handle)
                if e.kind == "worker_started"]


def test_sa01_actual_source_is_consumed_without_granting_work(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    before = len(provider.calls)
    startup.refresh()
    assert len(provider.calls) > before
    startup.finish()
    no_started_worker(provider, attempt)


def test_sa02_slow_startup_maintains_facts_beyond_original_receipt(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    rig = provider.rig
    first = rig.core.scheduling.explain(rig.handle, attempt.task_id)
    rig.now += 20
    startup.refresh()
    rig.now += 15
    assert rig.now > first["expires_at"]
    startup.check()
    assert startup.deadline == 1120
    no_started_worker(provider, attempt)


def test_sa03_expired_prior_observation_cannot_be_revived(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    before = len(provider.calls)
    provider.rig.now += 31
    with pytest.raises(SchedulingBlocked):
        startup.refresh()
    assert len(provider.calls) == before
    no_started_worker(provider, attempt)


def test_sa04_new_predecessor_fences_the_existing_reserved_attempt(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    predecessor = provider.task()
    provider.edge(predecessor, provider.rig.core.get_task(provider.rig.handle, attempt.task_id))
    with pytest.raises(SchedulingBlocked):
        startup.refresh()
    view = provider.rig.core.scheduling.explain(provider.rig.handle, attempt.task_id)
    assert view["state"] != "ready"
    no_started_worker(provider, attempt)


def test_sa05_stopped_attempt_cannot_restart_startup_observation(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    rig = provider.rig
    rig.settle(rig.current(rig.core.get_task(rig.handle, attempt.task_id)), attempt, "paused")
    with pytest.raises(AccessDenied):
        startup.refresh()


@pytest.mark.parametrize("field", ["deadline", "owner", "source"])
def test_sa06_original_owner_and_source_binding_drift_refuses(tmp_path, field):
    provider, startup, attempt = owned(tmp_path)
    if field == "deadline":
        startup.deadline += 1
    elif field == "owner":
        startup.owner = dict(startup.owner, worker_id="different-worker")
    else:
        startup.registry.dependencies = object()
    with pytest.raises(AccessDenied):
        startup.refresh()
    no_started_worker(provider, attempt)


def test_sa07_collection_failure_is_retained_and_helper_stops(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    provider.disabled = True
    startup.start()
    startup._thread.join(timeout=2)
    assert startup.close()
    with pytest.raises(AccessDenied, match="observation failed"):
        startup.finish()
    no_started_worker(provider, attempt)


def test_sa08_handover_waits_outside_core_and_consumes_inflight_observation(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    entered, release, finished = Event(), Event(), Event()
    errors = []

    def pause(query, variables):
        if "DependencySchema" in query:
            entered.set()
            assert release.wait(3)

    provider.callback = pause
    startup.start()
    assert entered.wait(2)

    def finish():
        try:
            startup.finish()
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    closer = Thread(target=finish)
    closer.start()
    # An independent Core connection remains usable while provider I/O waits.
    provider.rig.core.get_task(provider.rig.handle, attempt.task_id)
    release.set()
    assert finished.wait(3)
    closer.join(timeout=1)
    assert not errors and startup.close()
    no_started_worker(provider, attempt)


def test_sa03_collection_that_finishes_after_prior_expiry_is_rejected(tmp_path):
    provider, startup, attempt = owned(tmp_path)
    provider.rig.now += 20
    reached = []

    def expire(query, variables):
        if "DependencySchema" in query and not reached:
            reached.append(True)
            provider.rig.now += 11

    provider.callback = expire
    with pytest.raises(SchedulingBlocked):
        startup.refresh()
    assert reached == [True]
    no_started_worker(provider, attempt)


def test_sa09_collection_cost_cannot_cause_immediate_repeated_refresh(tmp_path, monkeypatch):
    from ai_task_manager.supervisor import startup_authority

    provider, startup, attempt = owned(tmp_path)
    ticks = iter((0.0, 16.0))
    monkeypatch.setattr(startup_authority, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    startup.start()
    startup._thread.join(timeout=2)
    assert startup.close()
    assert isinstance(startup._failure, SchedulingBlocked)
    assert "no safe next freshness window" in str(startup._failure)
    view = provider.rig.core.scheduling.explain(provider.rig.handle, attempt.task_id)
    assert view["source_revision"] == 2
    no_started_worker(provider, attempt)


def successor_startup(tmp_path):
    """Authored current authority over an actually settled Core predecessor."""
    import secrets

    from ai_task_manager.adapters.linear.dependencies.source import DependencySource
    from ai_task_manager.application.workflows.recovery import SuccessorAuthorization
    from ai_task_manager.domains.work.models import Evidence, SessionHandle

    provider = ProviderFixture(tmp_path / "provider")
    r = provider.rig
    task = provider.task(code=True)
    task = r.transition(task, IssueStatus.TODO, "enqueue")
    provider.sync(task)
    provider.refresh(task)
    task, prior = r.running(task)
    task, prior = r.settle(task, prior, "paused")
    provider.sync(task)
    foreign = provider.task()
    foreign = r.transition(foreign, IssueStatus.TODO, "enqueue")
    provider.sync(foreign)
    provider.refresh(foreign)
    foreign, foreign_attempt = r.reserved(foreign)
    decision = r.proof(
        "decision", task.id,
        {"target": "In Progress", "revision": task.decision_revision, "actor": "agent"},
        expires_at=r.now + 180,
    )
    authorization = SuccessorAuthorization(
        r.key(), r.scope, "workflow:predecessor", task.id, task.version,
        task.decision_revision, "fresh-plan", "fresh-digest", "fresh-key",
        r.now + 180, decision, tuple(task.required_work),
    )
    r.proofs.evidence[authorization.reference] = Evidence(
        "workflow_successor", r.scope, task.id, asdict(authorization), authorization.expires_at
    )
    handle = SessionHandle(secrets.token_urlsafe(32))
    r.core.revoke_session(r.handle)
    r.core.admit_workflow_successor(
        r.handle, handle, authorization, lambda *_: "workflow:successor"
    )
    source = DependencySource(r.core, handle, provider.client, provider.receipts)
    current = r.core.get_task(handle, task.id)
    source.refresh_for_start(
        task.id, "workflow:successor:start",
        expected_version=current.version, decision_revision=current.decision_revision,
    )
    current = r.core.get_task(handle, task.id)
    attempt = r.core.execution.reserve(
        handle, task.id, "host", current.version, current.decision_revision,
        decision, "workflow:successor:start:reserve",
    )
    runtime = RuntimeJournal(tmp_path / "runtime.sqlite", clock=lambda: r.now)
    runtime.claim(attempt, asdict(r.scope), deadline=r.now + 120)
    runtime.claim(foreign_attempt, asdict(r.scope), deadline=r.now + 120)
    registry = WorkAuthorities(r.core, source)
    r.core.scheduling.renewals = registry
    return provider, registry, handle, attempt, foreign_attempt, runtime


def test_sa10_current_successor_starts_from_retained_in_progress(tmp_path):
    provider, registry, handle, attempt, _, runtime = successor_startup(tmp_path)
    startup = registry.starting(handle, attempt, runtime, provider.rig.now + 120)
    assert startup.check().observed_status == "In Progress"
    before = len(provider.calls)
    startup.refresh()
    startup.finish()
    assert len(provider.calls) > before
    assert provider.rig.core.get_attempt(handle, attempt.id).worker_id is None


def test_sa11_ordinary_in_progress_still_refuses_startup(tmp_path):
    from dataclasses import replace

    from ai_task_manager.storage import work

    provider, startup, attempt = owned(tmp_path)
    r = provider.rig
    sid, _ = r.core.session_binding(r.handle)
    with r.db.transaction() as conn:
        task = work.get(conn, sid, attempt.task_id)
        work.save(conn, replace(task, observed_status="In Progress"), task.version)
    before = len(provider.calls)
    with pytest.raises(AccessDenied):
        startup.refresh()
    assert len(provider.calls) == before
    no_started_worker(provider, attempt)


def test_sa12_successor_cannot_start_a_foreign_task(tmp_path):
    provider, registry, handle, _, foreign, runtime = successor_startup(tmp_path)
    before = runtime.attempt(foreign.id)
    calls = len(provider.calls)
    with pytest.raises(AccessDenied):
        registry.starting(handle, foreign, runtime, provider.rig.now + 120)
    assert runtime.attempt(foreign.id) == before and len(provider.calls) == calls


@pytest.mark.parametrize(
    "variant", ["current", "provider-expired", "held", "source", "revision", "deadline"]
)
def test_sa10_declared_milestone_startup_keeps_original_provider_window(tmp_path, variant):
    import json
    import time

    from ai_task_manager.adapters.linear.dependencies.source import (
        DependencySource,
        milestone_declarations,
    )
    from ai_task_manager.domains.scheduling.resources import AdmissionResources
    from ai_task_manager.domains.work.models import IssueStatus as S
    from ai_task_manager.storage import work

    p = ProviderFixture(tmp_path / "provider")
    r = p.rig
    a = p.task(code=True, title="Code predecessor")
    task = p.task(title="Dependent startup")
    p.edge(a, task)
    declarations = milestone_declarations(
        [
            {
                "task_prose_digest": task.prose_digest,
                "predecessor_prose_digest": a.prose_digest,
                "condition": "code_merged",
            }
        ]
    )
    p.source = DependencySource(r.core, r.handle, p.client, p.receipts, milestones=declarations)
    for item in [a, task]:
        r.resource_sources.rows[item.id] = AdmissionResources(
            p.scope, item.id, item.prose_digest, item.decision_revision, "authored-project", (), ()
        )
    p.source.refresh(a.id, "predecessor")
    a = r.transition(a, S.TODO, "enqueue")
    p.sync(a)
    a, attempt = r.running(a)
    a = r.complete(a)
    a, _ = r.settle(a, attempt)
    reference = r.proof(
        "merge", a.id, {"commit": "a" * 40, "prose_digest": a.prose_digest}, expires_at=r.now + 5
    )
    a = r.transition(a, S.IN_DEV, "merge", {"merge": reference})
    p.sync(a)
    prepared = []

    def prepare(subject, ref):
        assert p.depth == 0 and subject == a.id and ref == reference
        r.proofs.evidence[ref] = replace(r.proofs.evidence[ref], expires_at=r.now + 5)
        prepared.append({"clock": r.now, "expiry": r.now + 5})
        return ref

    r.core.context.evidence.prepare_merge_milestone = prepare
    task = r.transition(task, S.TODO, "enqueue")
    p.sync(task)
    p.source.refresh(task.id, "dependent")
    task, attempt = r.reserved(r.current(task))
    runtime = RuntimeJournal(tmp_path / "runtime.sqlite", clock=lambda: r.now)
    runtime.claim(attempt, asdict(r.scope), deadline=r.now + 120)
    registry = WorkAuthorities(r.core, p.source)
    r.core.scheduling.renewals = registry
    startup = registry.starting(r.handle, attempt, runtime, r.now + 120)
    facts = {
        "scope": (
            "Actual StartupAuthority loop, real Core/provider reader; authored provider data "
            "and expiring proof producer, zero native/model effects"
        )
    }
    try:
        startup.start()
        end = time.monotonic() + 3
        while time.monotonic() < end:
            first = r.core.scheduling.explain(r.handle, task.id)
            if first["source_revision"] >= 2:
                break
            startup._thread.join(0.01)
        assert first["source_revision"] >= 2, first
        facts.update(
            before=first,
            verifier_clock_before=r.now,
            proof_expiry_before=r.proofs.evidence[reference].expires_at,
        )
        provider_calls = len(p.calls)
        r.now += 6
        if variant == "provider-expired":
            r.now = first["expires_at"]
        elif variant == "held":
            current = r.core.get_task(r.handle, task.id)
            with r.db.transaction() as conn:
                work.save(conn, replace(current, human_hold=True), current.version)
        elif variant == "source":
            p.source.source_id = "changed-source"
        elif variant == "revision":
            p.source.refresh(task.id, "independent-new-revision")
            provider_calls = len(p.calls)
        elif variant == "deadline":
            r.now = startup.deadline
        startup._thread.join(timeout=2)
        facts.update(
            verifier_clock_after=r.now,
            proof_expiry_after=r.proofs.evidence[reference].expires_at,
            after=r.core.scheduling.explain(r.handle, task.id),
            preparations=prepared,
            failure=None
            if startup._failure is None
            else {"type": type(startup._failure).__name__, "detail": str(startup._failure)},
            worker_id=r.core.get_attempt(r.handle, attempt.id).worker_id,
        )
        assert len(p.calls) == provider_calls
        if variant == "current":
            assert first["expires_at"] > r.now
            assert startup._failure is None, facts
            assert facts["proof_expiry_after"] == r.now + 5
            assert facts["after"]["expires_at"] == first["expires_at"]
            assert facts["after"]["source_revision"] == first["source_revision"]
            startup.check()
        else:
            assert startup._failure is not None, facts
        assert r.core.get_attempt(r.handle, attempt.id).worker_id is None
        assert not [
            e
            for e in r.core.notifications.list_events(r.handle)
            if e.kind == "worker_started" and e.task_id == task.id
        ]
    finally:
        startup.close()
        (tmp_path / "startup-milestone-result.json").write_text(json.dumps(facts, indent=2) + "\n")
