"""Elapsed startup over owned authored native identity and report-result controls."""

import time
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.evaluation_enrollment import EvaluationGrant
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.config.evaluation_records import PURPOSES, EvaluationRecords
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.bootstrap_helpers import grant_graph
from tests.hosts.test_evaluation_reports import authored_report
from tests.hosts.test_identity_observations import ScopedAuthPermits


def elapsed_report(make, monkeypatch):
    item = make(seconds=120)
    # A real local authored startup is observed with an earlier virtual timestamp,
    # then its unchanged account-status command issues genuinely fresh authority.
    item.journal.clock = lambda: time.time() - 31
    g = grant_graph(item)
    item.journal.clock = time.time
    identities = NativeIdentityObservations(
        item.native,
        ScopedAuthPermits(item),
        not_after=item.binding.deadline,
    )
    original = item.journal._read(g.body["bootstrap_ref"], "trace")
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
    issuer, oracle, pair, runner = authored_report(g, monkeypatch)
    identities.refresh(item.root / "current-report-auth")
    item.journal.clock = lambda: original["expires_at"] + 0.01
    assert item.journal.clock() < identities.prepare().observation.expires_at
    return g, issuer, oracle, pair, runner, identities, original


def test_report_after_elapsed_startup_uses_current_identity_and_immutable_history(
    bootstrap_graph,
    monkeypatch,
):
    g, issuer, oracle, pair, _, identities, original = elapsed_report(bootstrap_graph, monkeypatch)
    with pytest.raises(AccessDenied, match="stale"):
        g.item.journal._read(original["ref"], "trace")
    current = identities.prepare().observation
    facts = issuer.observe(oracle, pair)
    assert len(facts) == 2
    for ref in facts.values():
        fact = issuer.verify_evidence(ref)
        trace_ref = issuer._prepared[ref][-1]
        trace = g.item.journal._read(trace_ref, "trace")
        assert trace["sources"][1] == current.reference
        assert trace["payload"]["original_bootstrap_ref"] == original["ref"]
        assert trace["payload"]["original_bootstrap_sha256"] == digest(original)
        assert trace["payload"]["current_authority_ref"] == current.reference
        assert trace["expires_at"] == fact.expires_at
        assert fact.expires_at <= min(
            current.expires_at, identities.execution_not_after, g.grant.data["expires_at"]
        )
    assert g.item.journal._read(original["ref"], "trace", fresh=False) == original
    with pytest.raises(AccessDenied, match="stale"):
        g.item.journal._read(original["ref"], "trace")


@pytest.mark.parametrize(
    "change", ["expired-current", "revoked", "wrong-owner", "history", "expired-verifier"]
)
def test_report_current_authority_and_result_refusals_remain_strict(
    bootstrap_graph,
    monkeypatch,
    change,
):
    g, issuer, oracle, pair, runner, identities, original = elapsed_report(
        bootstrap_graph,
        monkeypatch,
    )
    reached = []
    if change == "expired-current":
        expiry = identities.prepare().observation.expires_at
        assert expiry < g.grant.data["expires_at"]
        g.item.journal.clock = lambda: expiry
    elif change == "revoked":
        identities.revoke()
    elif change == "wrong-owner":
        g.grant.identities = SimpleNamespace()
    elif change == "history":
        with g.item.journal.transaction() as conn:
            conn.execute("UPDATE host_records SET data=data || ' ' WHERE ref=?", (original["ref"],))
    else:
        now = g.item.journal.clock()
        previous = runner.jobs["authored"]["verified_ref"]
        payload = g.item.journal._read(previous, "trace")["payload"]
        shorter = g.item.journal._append("trace", payload, expires_at=now + 0.1)
        runner.jobs["authored"]["verified_ref"] = shorter
        g.item.journal.clock = lambda: now + 0.1
        verify = runner.verified_result

        def expired(*args):
            reached.append("actual-result-read")
            return verify(*args)

        monkeypatch.setattr(runner, "verified_result", expired)
    with pytest.raises(AccessDenied):
        issuer.observe(oracle, pair)
    if change == "expired-verifier":
        assert reached == ["actual-result-read"]
    assert issuer._prepared == {}
