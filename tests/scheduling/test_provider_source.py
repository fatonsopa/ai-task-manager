"""Real Core/client scheduling boundaries with authored, never live, Linear data."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.linear.dependencies.receipts import PREFIX
from ai_task_manager.adapters.linear.models import WireResponse
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, StaleVersion
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import work

from .provider_fixture import ProviderFixture


@pytest.fixture
def provider(tmp_path):
    return ProviderFixture(tmp_path / "owned")


def test_complete_empty_prepare_order_local_resolver_and_historical_retry(provider):
    p, r = provider, provider.rig
    task = p.task()
    prepared = p.source.prepare(task.id, "stable")
    assert r.core.scheduling.explain(r.handle, task.id)["state"] == "unknown"
    count = len(p.calls)
    p.disabled = True
    snapshot = p.receipts.verify_snapshot(prepared.snapshot_ref)
    assert snapshot.completeness == "complete" and snapshot.dependencies == ()
    result = p.source.refresh(task.id, "stable")
    assert result["state"] == "ready" and result["graph_version"] == 1
    events = r.core.notifications.list_events(r.handle)
    r.now += 31
    assert p.source.refresh(task.id, "stable") == result
    assert len(p.calls) == count and r.core.notifications.list_events(r.handle) == events


@pytest.mark.parametrize("kind", ["blocks", "related", "similar", "archived"])
def test_outgoing_and_known_nonblocking_relations_do_not_invent_predecessors(provider, kind):
    a, b = provider.task(), provider.task()
    edge = provider.edge(a, b, kind if kind != "archived" else "blocks")
    if kind == "archived":
        edge["archivedAt"] = edge["updatedAt"]
        result = provider.refresh(b)
    else:
        result = provider.refresh(a if kind == "blocks" else b)
    assert result["state"] == "ready"


def test_both_directions_complete_pagination_and_actual_predecessor_block(provider):
    a, b, root, successor = [provider.task() for _ in range(4)]
    provider.edge(a, root)
    provider.edge(b, root)
    provider.edge(root, successor)
    provider.page_size = 1
    result = provider.refresh(root)
    assert result["state"] == "blocked"
    assert {row["dependency"] for row in result["blockers"]} == {a.id, b.id}
    assert any(v.get("after") == "1" for q, v in provider.calls if "inverseRelations(" in q)
    assert any("relations(" in q for q, _ in provider.calls)


@pytest.mark.parametrize("variant", ["principal", "project", "team", "archived", "missing"])
def test_root_metadata_scope_refuses_before_prose_or_relationships(provider, variant):
    task = provider.task()
    node = provider.nodes[task.issue_id]
    if variant == "principal":
        node["assignee"]["id"] = str(uuid4())
    elif variant == "project":
        node["project"]["id"] = str(uuid4())
    elif variant == "team":
        node["team"]["id"] = str(uuid4())
    elif variant == "archived":
        node["archivedAt"] = node["updatedAt"]
    else:
        node.pop("archivedAt")
    assert provider.refresh(task)["state"] == "unknown"
    assert not any("DependencyProse" in q or "Relations(" in q for q, _ in provider.calls)


def test_cross_session_predecessor_is_unresolved_without_foreign_prose(provider):
    r = provider.rig
    foreign = r.session(replace(provider.scope, logical_session="other"))
    predecessor, task = provider.task(handle=foreign), provider.task()
    provider.edge(predecessor, task)
    assert provider.refresh(task)["state"] == "unknown"
    assert not any(
        "DependencyProse" in q and v.get("id") == predecessor.issue_id for q, v in provider.calls
    )


@pytest.mark.parametrize(
    "variant", ["prose", "status", "unknown_relation", "duplicate", "cycle", "bad_schema"]
)
def test_ambiguous_inputs_and_cycle_never_become_empty_ready(provider, variant):
    a, task = provider.task(), provider.task()
    provider.edge(a, task)
    if variant == "prose":
        provider.nodes[a.issue_id]["description"] = "changed"
    elif variant == "status":
        provider.nodes[a.issue_id]["state"] = {"id": provider.states["Done"], "name": "Done"}
    elif variant == "unknown_relation":
        provider.edges[0]["type"] = "new-kind"
    elif variant == "duplicate":
        provider.edges[0]["type"] = "duplicate"
    elif variant == "cycle":
        provider.edge(task, a)
    else:
        provider.schema["relationTypes"]["enumValues"].append({"name": "future"})
    assert provider.refresh(task)["state"] == "unknown"


@pytest.mark.parametrize(
    "variant", ["page", "repeat_cursor", "duplicate_id", "revision", "second_scan", "direction"]
)
def test_partial_pagination_and_changing_graph_block(provider, variant):
    a, b, task = provider.task(), provider.task(), provider.task()
    provider.edge(a, task)
    provider.edge(b, task)
    provider.page_size = 1
    original = provider.send
    count = 0

    def altered(query, variables, credentials):
        nonlocal count
        if (
            variant == "second_scan"
            and count == 2
            and "inverseRelations(" in query
            and variables["id"] == task.issue_id
        ):
            for edge in provider.edges:
                edge["updatedAt"] = "2026-11-02T00:00:01Z"
        response = original(query, variables, credentials)
        data = json.loads(response.body)
        if "inverseRelations(" in query and variables["id"] == task.issue_id:
            count += 1
            raw = data["data"]["issue"]
            connection = raw["inverseRelations"]
            if variant == "page":
                connection.pop("pageInfo")
            elif variant == "repeat_cursor":
                connection["pageInfo"] = {"hasNextPage": True, "endCursor": "1"}
            elif variant == "duplicate_id" and count == 2:
                connection["nodes"][0]["id"] = provider.edges[0]["id"]
            elif variant == "revision" and count == 2:
                raw["updatedAt"] = "2026-11-02T00:00:01Z"
            elif variant == "direction":
                connection["nodes"][0]["relatedIssue"]["id"] = a.issue_id
        return WireResponse(200, {}, json.dumps(data).encode())

    provider.send = altered
    assert provider.refresh(task)["state"] == "unknown"


@pytest.mark.parametrize("kind", ["Canceled", "Duplicate", "held", "failed", "Done"])
def test_provider_state_does_not_create_completion_proof(provider, kind):
    a, task = provider.task(), provider.task()
    current = a
    if kind in {"Canceled", "Duplicate", "Done"}:
        current = replace(a, observed_status=S(kind), desired_status=S(kind))
    elif kind == "held":
        current = replace(a, human_hold=True)
    else:
        current = replace(a, execution_state="failed")
    # Explicit authored stored boundary fixture; no fabricated provider completion proof.
    with provider.rig.db.transaction() as conn:
        current = work.save(conn, current, a.version)
    provider.sync(current)
    provider.edge(current, task)
    result = provider.refresh(task)
    assert result["state"] == "blocked"
    assert result["blockers"][0]["code"] != "ready"


def test_real_core_noncode_completion_refs_allow_dependency(provider):
    r = provider.rig
    a = provider.task()
    provider.refresh(a)
    a = r.transition(a, S.TODO, "enqueue")
    provider.sync(a)
    a, attempt = r.running(a)
    a = r.complete(a)
    a, _ = r.settle(a, attempt)
    a = r.transition(a, S.DONE, "non_code_complete", r.delivery(a))
    provider.sync(a)
    task = provider.task()
    provider.edge(a, task)
    assert provider.refresh(task)["state"] == "ready"


@pytest.mark.parametrize("kind", ["version", "decision", "session", "generation"])
def test_concurrent_local_or_collection_changes_refuse(provider, kind):
    task = provider.task()
    changed = False

    def callback(query, variables):
        nonlocal changed
        if changed or "DependencyMetadata" not in query:
            return
        changed = True
        if kind == "session":
            provider.source.binding = ("changed", provider.scope)
        elif kind == "generation":
            provider.receipts.begin(provider.scope, task, 0, "competing")
        else:
            with provider.rig.db.transaction() as conn:
                value = replace(task, decision_revision=1) if kind == "decision" else task
                work.save(conn, value, task.version)

    provider.callback = callback
    with pytest.raises((AccessDenied, StaleVersion)):
        provider.refresh(task)
    assert provider.rig.core.scheduling.explain(provider.rig.handle, task.id)["state"] == "unknown"


@pytest.mark.parametrize("kind", ["forged", "tampered", "expired", "wrong_purpose", "superseded"])
def test_immutable_receipts_refuse_invalid_or_stale_inputs(provider, kind):
    task = provider.task()
    prepared = provider.source.prepare(task.id, "receipt")
    ref = prepared.snapshot_ref
    if kind == "forged":
        ref = PREFIX + str(uuid4())
    elif kind == "tampered":
        with provider.receipts.transaction() as conn:
            conn.execute("UPDATE records SET data='{}' WHERE ref=?", (ref,))
    elif kind == "expired":
        provider.rig.now += 31
    elif kind == "wrong_purpose":
        raw = provider.receipts._get(ref, PREFIX)
        raw["purpose"] = "other"
        ref = provider.receipts._put(PREFIX, raw)
    else:
        provider.receipts.begin(provider.scope, task, 0, "newer")
    with pytest.raises(AccessDenied):
        provider.receipts.verify_snapshot(ref)
    assert provider.rig.core.scheduling.explain(provider.rig.handle, task.id)["state"] == "unknown"


@pytest.mark.parametrize(
    "priority,expected",
    [
        (0, "none"),
        (1, "urgent"),
        (2, "high"),
        (3, "normal"),
        (4, "low"),
        (None, None),
        (True, None),
        (5, None),
    ],
)
def test_exact_priority_mapping(provider, priority, expected):
    task = provider.task()
    provider.nodes[task.issue_id]["priority"] = priority
    result = provider.refresh(task)
    assert result["state"] == ("ready" if expected else "unknown")
    if expected:
        assert result["priority"] == expected


def service_fixture(p, root, *, fail_first=False):
    r = p.rig
    journal = AuthorityJournal(root / "service.sqlite", root / "service.key")
    calls = []

    def start(handle, task_id, host, **values):
        calls.append(r.core.scheduling.explain(handle, task_id))
        assert calls[-1]["state"] == "ready"
        if fail_first and len(calls) == 1:
            raise EvidenceRequired("Authored pre-reserve refusal")
        return r.core.execution.reserve(
            handle,
            task_id,
            host,
            values["expected_version"],
            values["decision_revision"],
            values["decision_ref"],
            values["operation_key"] + ":reserve",
        )

    decisions = SimpleNamespace(
        journal=journal, decision=lambda scope, task, target, action, **kw: r.decision(task, target)
    )
    service = ApplicationService(
        r.core,
        r.handle,
        decisions=decisions,
        workers=SimpleNamespace(start=start),
        dependencies=p.source,
        grant_ref="authored",
    )
    return service, calls


@pytest.mark.parametrize("kind", ["valid_cached", "refused_retry", "stale", "foreign", "blocked"])
def test_normal_worker_start_refresh_order_and_retry(provider, tmp_path, kind):
    p, r = provider, provider.rig
    task = p.task()
    task = r.transition(task, S.TODO, "enqueue")
    p.sync(task)
    if kind == "blocked":
        p.edge(p.task(), task)
    service, calls = service_fixture(p, tmp_path / "owned", fail_first=kind == "refused_retry")
    data = {
        "task_id": task.id,
        "host": "host",
        "expected_version": task.version,
        "decision_revision": task.decision_revision,
        "operation_key": "start",
    }
    if kind == "stale":
        data["expected_version"] -= 1
    if kind == "foreign":
        other = r.session(replace(p.scope, logical_session="other"))
        data["task_id"] = p.task(handle=other).id
    before = len(p.calls)
    first = service.call("worker_start", data)
    if kind in {"stale", "foreign", "blocked"}:
        assert not first["ok"] and calls == []
        with r.db.transaction() as conn:
            assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        if kind in {"stale", "foreign"}:
            assert len(p.calls) == before
        return
    if kind == "refused_retry":
        assert not first["ok"] and len(calls) == 1
        before = len(p.calls)
        r.now += 31
        second = service.call("worker_start", data)
        assert second["ok"] and len(p.calls) > before and len(calls) == 2
    else:
        assert first["ok"] and len(calls) == 1
        before = len(p.calls)
        p.disabled = True
        r.now += 31
        second = service.call("worker_start", data)
        assert second["ok"] and second["data"]["id"] == first["data"]["id"]
        assert len(p.calls) == before and len(calls) == 1
