"""Prepared decision lifecycle over real Core and owned authored CLI controls."""

import time
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.accounting import (
    AccountingPlan,
    HostInvocationAccounting,
)
from ai_task_manager.adapters.hosts.bridge.admission import NativeAdmissionVerifier
from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.evaluation_decisions import EvaluationDecisions
from ai_task_manager.adapters.hosts.bridge.evaluation_enrollment import EvaluationGrant
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.adapters.hosts.bridge.policy import NativePolicy, NativeProfile, policy_digest
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.config.evaluation_records import PURPOSES, EvaluationRecords
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.managed import ManagedWorkers
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.bootstrap_helpers import grant_graph
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_evaluation_enrollment import enroll, task
from tests.hosts.test_identity_observations import ScopedAuthPermits
from tests.runtime.helpers import ProviderFixture, RuntimeRig
from tests.scheduling.helpers import SnapshotSource, declare


def graph(make):
    item = make(seconds=120)
    item.journal.clock = lambda: time.time() - 31
    g = grant_graph(item)
    item.journal.clock = time.time
    identities = NativeIdentityObservations(
        item.native,
        ScopedAuthPermits(item),
        not_after=item.binding.deadline,
    )
    body = dict(
        g.body,
        version=2,
        identity_mode="fresh-same-native-account",
        expires_at=identities.execution_not_after,
    )
    ref = item.accounting.rig.pin("evaluation_grant", body)
    records = EvaluationRecords(
        {k: v for k, v in item.accounting.rig.bindings.items() if v["kind"] in PURPOSES}
    )
    g.grant = EvaluationGrant(
        records,
        ref,
        "policy-fixture",
        item.observations,
        g.core,
        g.claim,
        item.journal,
        item.expected,
        identities=identities,
    )
    g.ref, g.body = ref, body
    g.handle = enroll(g)
    g.task = task(g, g.handle)
    g.proofs = g.core.context.evidence
    g.decisions = EvaluationDecisions(g.grant, g.authority)
    g.core.context.clock = item.journal.clock
    g.authority.clock = lambda: item.journal.clock()
    g.core.context.evidence = EvidenceRouter(
        [
            EvidenceRoute("decision-proof:", frozenset({"decision"}), g.decisions),
            EvidenceRoute("fixture:", frozenset({"provider_status"}), g.proofs),
        ]
    )
    g.identities, g.item = identities, item
    return g


def issue(g, target="Todo", action="enqueue"):
    return g.decisions.decision(g.grant.scope, g.task, target, action, grant_ref=g.ref)


def prepare(g, ref, **changes):
    values = dict(
        handle=g.handle,
        task_id=g.task.id,
        expected_version=g.task.version,
        decision_revision=g.task.decision_revision,
    )
    values.update(changes)
    return g.decisions.prepare_consumption(ref, **values)


def test_cached_identity_stays_expired_but_same_decision_can_prepare_current(bootstrap_graph):
    g = graph(bootstrap_graph)
    ref = issue(g)
    old = g.decisions._prepared[ref]
    identity = old.identity.observation
    g.identities.refresh(g.item.root / "new-auth")
    g.item.journal.clock = lambda: identity.expires_at + 0.01
    with pytest.raises(AccessDenied, match="unexpired issued"):
        g.decisions.verify_evidence(ref)
    original = g.authority.verify_evidence(ref)
    prepare(g, ref)
    assert g.authority.verify_evidence(ref) == original
    assert g.decisions.verify_evidence(ref) == original
    with pytest.raises(AccessDenied):
        old.check()
    assert g.grant.data["expires_at"] == g.identities.execution_not_after


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "revoked",
        "expired",
        "source",
        "profile",
        "task",
        "handle",
        "version",
        "revision",
        "reference",
    ],
)
def test_reprepare_refuses_changed_or_missing_current_authority(bootstrap_graph, change):
    g = graph(bootstrap_graph)
    ref = issue(g)
    values = {}
    if change == "owner":
        g.grant.identities = SimpleNamespace()
    elif change == "revoked":
        g.identities.revoke()
    elif change == "expired":
        # Capture the actual original expiry without recursively querying the observer.
        expiry = g.identities._current.expires_at
        g.item.journal.clock = lambda: expiry
    elif change == "source":
        g.grant.source.path.write_bytes(g.grant.source.path.read_bytes() + b"\n")
    elif change == "profile":
        object.__setattr__(g.item.policy, "model", "foreign")
    elif change == "task":
        values["task_id"] = "foreign"
    elif change == "handle":
        values["handle"] = "foreign"
    elif change == "version":
        values["expected_version"] = g.task.version + 1
    elif change == "revision":
        values["decision_revision"] = g.task.decision_revision + 1
    else:
        ref = "decision-proof:foreign"
    with pytest.raises(AccessDenied):
        prepare(g, ref, **values)


@pytest.mark.parametrize("enabled", [False, True])
def test_managed_start_reprepares_after_native_startup_across_old_identity_expiry(
    bootstrap_graph,
    tmp_path,
    monkeypatch,
    enabled,
):
    g = graph(bootstrap_graph)
    rr = RuntimeRig(tmp_path / "worker-runtime")
    r = rr.rig
    r.core, r.handle, r.scope, r.db, r.proofs = (
        g.core,
        g.handle,
        g.grant.scope,
        g.core.context.db,
        g.proofs,
    )
    r.now = time.time()
    rr.journal.clock = lambda: g.item.journal.clock()
    g.core.context.clock = rr.journal.clock
    source = SnapshotSource()
    g.core.scheduling.sources = source
    # Real Core transitions use authored provider facts, never live Linear evidence.
    ref = issue(g)
    g.task = g.core.transitions.transition(
        g.handle,
        g.task.id,
        "Todo",
        "enqueue",
        g.task.version,
        g.task.decision_revision,
        ref,
        {},
        "queue",
    )
    g.task = r.confirm(g.task)
    declare(g.core, g.handle, g.task, source, rr.journal.clock())
    deadline = g.identities.execution_not_after
    plan = AccountingPlan(
        "batch",
        "decision-worker",
        g.task.id,
        "claude",
        g.item.policy.model,
        "high",
        g.body["billing_basis_ref"],
        deadline,
        85,
        10,
        15,
        20,
    )
    accounting = HostInvocationAccounting(
        g.item.accounting.control,
        g.core,
        g.handle,
        rr.journal,
        g.item.journal,
        plan,
        admissions=NativeAdmissionVerifier(g.item.observations, g.body["bootstrap_ref"], g.grant),
    )

    def policy(host, pair):
        old = g.item.policy
        environment = dict(old.environment)
        return NativePolicy(
            host,
            old.executable,
            old.executable_sha256,
            environment,
            pair.worktree,
            policy_digest(
                host,
                old.executable_sha256,
                environment,
                pair.worktree,
                {},
                model=old.model,
                effort=old.effort,
            ),
            old.seals,
            old.allowed_tools,
            old.model,
            old.effort,
        )

    factory = ManagedHostFactory(
        g.core,
        rr.resources,
        g.item.journal,
        tmp_path / "worker",
        policy,
        accounting,
        {"claude": g.item.native.expected_account},
        model_profiles={"claude": NativeProfile(g.item.policy.model, "high")},
    )
    proofs = RuntimeProofs(
        rr.journal, tmp_path / "proof.key", host_startups=factory.startup_verifier
    )
    g.core.context.evidence = EvidenceRouter(
        [
            EvidenceRoute("decision-proof:", frozenset({"decision"}), g.decisions),
            EvidenceRoute("fixture:", frozenset({"provider_status"}), g.proofs),
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
        ]
    )
    workers = ManagedWorkers(
        g.core,
        rr.journal,
        rr.resources,
        rr.runtime,
        proofs,
        ProviderFixture(r),
        factory,
        deadline=deadline,
        principal_ref=g.ref,
        decision_preparation=g.decisions if enabled else None,
    )
    factory.commands = workers
    ref = issue(g, "In Progress", "start")
    old = g.decisions._prepared[ref].identity
    g.identities.refresh(g.item.root / "fresh-before-worker")
    launch = factory.launch
    reached = []

    def wrapped_launch(binding, pair):
        native = launch(binding, pair)
        initialize = native.initialize

        def delayed():
            ready = initialize()
            g.item.journal.clock = lambda: old.observation.expires_at + 0.01
            r.now = rr.journal.clock()
            current = g.core.get_task(g.handle, g.task.id)
            declare(g.core, g.handle, current, source, rr.journal.clock())
            reached.append("native-ready-and-old-identity-expired")
            return ready

        native.initialize = delayed
        return native

    monkeypatch.setattr(factory, "launch", wrapped_launch)
    try:
        args = dict(
            expected_version=g.task.version,
            decision_revision=g.task.decision_revision,
            decision_ref=ref,
            operation_key="managed-decision",
        )
        if enabled:
            attempt = workers.start(g.handle, g.task.id, "claude", **args)
            assert attempt.state == "running"
            assert reached == ["native-ready-and-old-identity-expired"]
            assert g.decisions._prepared[ref].identity.observation is not old.observation
            assert workers.stop(g.handle, attempt.id, operation_key="stop").state == "paused"
        else:
            with pytest.raises(AccessDenied, match="unexpired issued"):
                workers.start(g.handle, g.task.id, "claude", **args)
            assert reached == ["native-ready-and-old-identity-expired"]
            assert not [
                e for e in g.core.notifications.list_events(g.handle) if e.kind == "worker_started"
            ]
    finally:
        for native in factory.owned.values():
            if native.process is not None and not native.process._closed:
                assert native.stop().confirmed
        rr.close()


def test_managed_constructor_preparation_owner_cannot_be_replaced(composition):
    runtime, current, decision, workers, _, _, _ = composition()
    before = runtime.rig.core.get_task(runtime.rig.handle, current.id)
    workers.decision_preparation = SimpleNamespace(prepare_consumption=lambda *a, **k: None)
    with pytest.raises(AccessDenied, match="preparation owner changed"):
        workers.start(
            runtime.rig.handle,
            current.id,
            "codex",
            expected_version=current.version,
            decision_revision=current.decision_revision,
            decision_ref=decision,
            operation_key="changed-preparer",
        )
    assert runtime.rig.core.get_task(runtime.rig.handle, current.id) == before
    with runtime.rig.db.transaction() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (current.id,)).fetchone()[
                0
            ]
            == 0
        )
