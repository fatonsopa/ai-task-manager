"""V2 consumers over real owned authored processes; no account/model acceptance."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity
from ai_task_manager.adapters.hosts.bridge.evaluation_enrollment import EvaluationGrant
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
from ai_task_manager.config.evaluation_records import PURPOSES, EvaluationRecords
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.bootstrap_helpers import grant_graph
from tests.hosts.test_accounting_adapter import graph as graph
from tests.hosts.test_identity_observations import ScopedAuthPermits


def v2(bootstrap_graph):
    item = bootstrap_graph(seconds=120)
    value = grant_graph(item)
    permits = ScopedAuthPermits(item)
    identities = NativeIdentityObservations(item.native, permits, not_after=item.binding.deadline)
    original = identities.prepare().observation
    identities.refresh(item.root / "fresh-v2")
    body = dict(value.body, version=2, identity_mode="fresh-same-native-account",
                expires_at=identities.execution_not_after)
    reference = item.accounting.rig.pin("evaluation_grant", body)
    records = EvaluationRecords(
        {k: v for k, v in item.accounting.rig.bindings.items() if v["kind"] in PURPOSES}
    )
    grant = EvaluationGrant(
        records, reference, "policy-fixture", item.observations, value.core,
        value.claim, item.journal, item.expected, identities=identities,
    )
    return item, value, identities, original, grant, records, reference


def test_v202_v2_grant_survives_original_proof_only_with_current_actual_auth(bootstrap_graph):
    item, old, identities, original, grant, _, _ = v2(bootstrap_graph)
    original_record = item.journal._read(item.native.startup.ready_ref)
    item.journal.clock = lambda: original.expires_at + 0.1
    with pytest.raises(AccessDenied):
        old.grant.require_active()
    observed = grant.require_active()
    assert observed["expires_at"] <= original.expires_at
    assert item.journal._read(item.native.startup.ready_ref, fresh=False) == original_record
    prepared = grant.prepare_current()
    prepared.check()
    identities.revoke()
    with pytest.raises(AccessDenied):
        prepared.check()


def test_v201_205_continuity_v2_consumes_new_identity_without_reviving_expired_work(
    bootstrap_graph,
):
    item, _, identities, original, _, _, _ = v2(bootstrap_graph)
    legacy = NativeContinuity(item.native)
    current = NativeContinuity(item.native, identities=identities)
    item.journal.clock = lambda: original.expires_at + 0.1
    with pytest.raises(AccessDenied):
        legacy.prepare()
    prepared, _ = current.prepare()
    current.acknowledge(prepared)
    assert current.identity_not_after == original.original_identity_not_after
    assert prepared.observed.expires_at <= current.execution_not_after
    item.journal.clock = lambda: current.expires_at
    with pytest.raises(AccessDenied):
        current.prepare()


@pytest.mark.parametrize("version,source", [(1, "real"), (2, "missing"), (2, "fake")])
def test_v210_identity_opt_in_cannot_be_a_boolean_or_missing_port(bootstrap_graph, version, source):
    item, value, identities, _, _, records, reference = v2(bootstrap_graph)
    if version == 1:
        records, reference = value.records, value.ref
    chosen = identities if source == "real" else None if source == "missing" else SimpleNamespace()
    with pytest.raises(AccessDenied):
        EvaluationGrant(
            records, reference, "policy-fixture", item.observations, value.core,
            value.claim, item.journal, item.expected, identities=chosen,
        )


def test_v202_grant_and_parent_execution_caps_remain_fixed(bootstrap_graph):
    item, _, identities, _, grant, _, _ = v2(bootstrap_graph)
    assert grant.data["expires_at"] == identities.execution_not_after
    item.journal.clock = lambda: grant.data["expires_at"]
    with pytest.raises(AccessDenied):
        grant.require_active()


def test_v206_prepared_v2_consumer_refuses_identity_owner_replacement(bootstrap_graph):
    _, _, _, _, grant, _, _ = v2(bootstrap_graph)
    prepared = grant.prepare_current()
    grant.identities = SimpleNamespace()
    with pytest.raises(AccessDenied):
        prepared.check()


def test_v212_issued_previous_identity_remains_valid_only_until_its_own_expiry(bootstrap_graph):
    item, _, identities, _, _, _, _ = v2(bootstrap_graph)
    previous = identities.prepare()
    identities.refresh(item.root / "another-v2")
    previous.check()
    item.journal.clock = lambda: previous.observation.expires_at
    with pytest.raises(AccessDenied):
        previous.check()


def test_v203_real_accounting_claim_clamps_identity_to_current_proof(graph):
    g = graph
    parent = g.adapter.authorize_initialization(g.policy, g.binding)
    cap = g.core.now + 3
    permit = g.adapter._claim(
        g.policy, g.binding, "auth", 15, reference="identity-1",
        parent=parent.native.reference, not_after=cap,
    )
    assert permit.binding.deadline <= cap
    call = next(c for c in g.ledger.current.snapshot()["calls"]
                if c["call_id"] == permit.reference)
    assert call["metadata"]["parent_call_id"] == parent.native.reference
    with pytest.raises(AccessDenied):
        g.adapter._claim(g.policy, g.binding, "auth", 15, reference="identity-1")


def test_v203_duplicate_or_reserved_refresh_names_refuse(graph):
    with pytest.raises(AccessDenied):
        replace(graph.plan, identity_refreshes=("one", "one"))
    with pytest.raises(AccessDenied):
        replace(graph.plan, identity_refreshes=("host",))


def test_v207_finite_loop_uses_no_refresh_when_proof_covers_execution_cap(bootstrap_graph):
    item = bootstrap_graph()
    item.bootstrap.start()
    identities = NativeIdentityObservations(
        item.native, ScopedAuthPermits(item), not_after=item.binding.deadline,
    )
    loop = IdentityRefresh(identities, item.root / "identity-loop", 1)
    before = item.accounting.number
    loop.start()
    loop.ensure_current()
    assert item.accounting.number == before
    assert loop.close()
    with pytest.raises(AccessDenied):
        loop.ensure_current()


def test_v204_observed_account_mismatch_stickily_fences_prepared_authority(bootstrap_graph):
    item = bootstrap_graph("account-change")
    item.bootstrap.start()
    observer = NativeIdentityObservations(
        item.native, ScopedAuthPermits(item), not_after=item.binding.deadline,
    )
    prepared = observer.prepare()
    (item.policy.cwd / "account-switch").write_text("authored account change")
    with pytest.raises(AccessDenied, match="account differs"):
        observer.refresh(item.root / "changed-account")
    with pytest.raises(AccessDenied):
        prepared.check()


def test_v207_due_refresh_uses_one_actual_probe_then_refuses_exhausted_budget(bootstrap_graph):
    item = bootstrap_graph(seconds=120)
    item.bootstrap.start()
    permits = ScopedAuthPermits(item)
    permits.maximum = 45  # Bounded fixture permit; the probe still has its 15-second cap.
    observer = NativeIdentityObservations(item.native, permits, not_after=item.binding.deadline)
    loop = IdentityRefresh(observer, item.root / "due-loop", 1)
    original = observer.prepare().observation
    item.journal.clock = lambda: original.expires_at - 45
    loop._latency = 22  # Authored previous measurement makes the first probe due.
    before = item.accounting.number
    prepared = loop.ensure_current()
    assert item.accounting.number == before + 1
    loop.ensure_current()
    assert item.accounting.number == before + 1
    item.journal.clock = lambda: prepared.observation.expires_at - 45
    with pytest.raises(AccessDenied, match="plan is exhausted"):
        loop.ensure_current()
    with pytest.raises(AccessDenied):
        prepared.check()
    assert loop.close()


def test_v207_unsafe_measured_window_refuses_without_immediate_second_probe(
    bootstrap_graph, monkeypatch,
):
    from ai_task_manager.adapters.hosts.bridge import identity_refresh

    item = bootstrap_graph(seconds=120)
    item.bootstrap.start()
    observer = NativeIdentityObservations(
        item.native, ScopedAuthPermits(item), not_after=item.binding.deadline,
    )
    loop = IdentityRefresh(observer, item.root / "unsafe-loop", 2)
    loop._latency = 30
    ticks = iter((0.0, 31.0))
    monkeypatch.setattr(identity_refresh, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    before = item.accounting.number
    with pytest.raises(AccessDenied, match="no safe next observation window"):
        loop.ensure_current()
    assert item.accounting.number == before + 1
    assert loop.close()
