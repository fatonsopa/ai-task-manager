"""Local case declarations are explicit test authority, not provider graph reads."""

from dataclasses import asdict

import pytest

from ai_task_manager.adapters.hosts.bridge.evaluation_scheduling import EvaluationScheduling
from ai_task_manager.config.evaluation_records import PURPOSES, EvaluationRecords
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.bootstrap_helpers import grant_graph
from tests.hosts.test_evaluation_enrollment import enroll, task


def declaration(g, item, **changes):
    session, scope = g.core.session_binding(g.grant._handle)
    graph = g.core.scheduling.explain(g.grant._handle, item.id)
    data = {
        "grant_ref": g.ref,
        "case_id": g.grant.data["case_id"],
        "scope": asdict(scope),
        "session_id": session,
        "claim_store": g.claim.context,
        "task_id": item.id,
        "issue_id": item.issue_id,
        "prose_digest": item.prose_digest,
        "source_revision": 1,
        "previous_graph_version": graph["graph_version"],
        "observed_at": g.item.journal.clock(),
        "expires_at": g.grant.data["expires_at"],
        "completeness": "complete",
        "dependencies": [],
        "priority": "normal",
    } | changes
    ref = g.item.accounting.rig.pin("evaluation_dependencies", data)
    records = EvaluationRecords(
        {k: v for k, v in g.item.accounting.rig.bindings.items() if v["kind"] in PURPOSES}
    )
    return ref, EvaluationScheduling(g.grant, records)


def test_declared_empty_case_graph_uses_real_core_without_provider_claim(bootstrap_graph):
    g = grant_graph(bootstrap_graph())
    handle = enroll(g)
    item = task(g, handle)
    ref, source = declaration(g, item)
    g.core.scheduling.sources = source
    source.prepare(ref)
    observed = g.core.scheduling.observe(
        handle, item.id, item.version, item.decision_revision, ref, "evaluation-dependencies"
    )
    assert observed["state"] == "ready"
    assert source.verify_snapshot(ref).source_id.startswith("lead-evaluation-case:")
    with pytest.raises(AccessDenied):
        source.prepare(ref)


@pytest.mark.parametrize(
    "change", ["task", "prose", "graph", "dependencies", "expiry", "source_revision"]
)
def test_changed_incomplete_or_stale_declaration_refuses(bootstrap_graph, change):
    g = grant_graph(bootstrap_graph())
    handle = enroll(g)
    item = task(g, handle)
    changes = {
        "task": {"task_id": "foreign"},
        "prose": {"prose_digest": "foreign"},
        "graph": {"previous_graph_version": 100},
        "dependencies": {"dependencies": ["foreign"]},
        "expiry": {"expires_at": 1},
        "source_revision": {"source_revision": True},
    }[change]
    ref, source = declaration(g, item, **changes)
    with pytest.raises(AccessDenied):
        source.prepare(ref)


def test_changed_source_after_prepare_is_refused_without_content_reads(
    bootstrap_graph, monkeypatch
):
    g = grant_graph(bootstrap_graph())
    handle = enroll(g)
    item = task(g, handle)
    ref, source = declaration(g, item)
    source.prepare(ref)
    _, seal, _, _ = source._prepared[ref]
    seal.path.write_bytes(seal.path.read_bytes() + b"\n")

    def forbidden(*args, **kwargs):
        raise AssertionError("Full source read under Core")

    monkeypatch.setattr(source.records, "resolve", forbidden)
    with g.core.context.db.transaction(), pytest.raises(AccessDenied):
        source.verify_snapshot(ref)
